"""Inbound local AGI/EAGI worker. stdout belongs to Asterisk."""
from array import array
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import wave

from .consent import ConfirmedDialogue
from .consultation import Consultation
from .crm import OperationStore
from .domain import FinalInput, SessionContext, SessionState, handle_input
from .quotes import FixtureQuoteAdapter
from .review import ReviewStore


class Hangup(Exception):
    pass


class AGI:
    def __init__(self, source, sink):
        self.source, self.sink = source, sink
        self.audio_fd = None
        while line := self.line():  # do not retain caller IDs
            if line == 'agi_enhanced: 1.0':
                self.audio_fd = 3
                os.set_blocking(3, False)

    def line(self):
        line = self.source.readline()
        if not line or line.strip() == 'HANGUP' or line.startswith('511 '):
            raise Hangup
        return line.rstrip('\r\n')

    def command(self, command):
        print(command, file=self.sink, flush=True)
        line = self.line()
        if '(hangup)' in line:
            raise Hangup
        match = re.fullmatch(r'200 result=(-?\d+)(?: .*)?', line)
        if not match:
            raise RuntimeError('AGI protocol error')
        return int(match[1])

    def up(self):
        if self.command('CHANNEL STATUS') != 6:  # answered channel
            raise Hangup

    def play(self, base):
        if self.command(f'STREAM FILE "{base}" ""') != 0:
            self.up()  # -1 can also mean an unreadable file
            raise RuntimeError('incomplete playback')

    def read_audio(self):
        data = bytearray()
        while True:
            try:
                block = os.read(self.audio_fd, 8192)
            except BlockingIOError:
                return data
            if not block:
                raise Hangup
            data.extend(block)

    def play_prompt(self, base, *, detect=True):
        if self.audio_fd is None:
            self.play(base)
            return True

        # res_agi forwards incoming voice to fd 3 during STREAM FILE. Use
        # bounded files so we never have to cancel an in-flight AGI command.
        part = base.with_name('segment')
        pending = bytearray()
        voiced = 0
        self.read_audio()  # discard input queued before playback
        with wave.open(str(base.with_suffix('.wav')), 'rb') as src:
            if (src.getframerate(), src.getnchannels(), src.getsampwidth()) != (8000, 1, 2) or not src.getnframes():
                raise ValueError('expected nonempty 8 kHz mono PCM prompt')
            remaining = src.getnframes()
            while remaining:
                count = min(remaining, 1600)  # at most 200 ms
                frames = src.readframes(count)
                if len(frames) != count * 2:
                    raise ValueError('truncated prompt')
                remaining -= count
                with wave.open(str(part.with_suffix('.wav')), 'wb') as dst:
                    dst.setparams((1, 2, 8000, count, 'NONE', 'not compressed'))
                    dst.writeframes(frames)
                self.play(part)
                audio = self.read_audio()
                if detect:
                    pending.extend(audio)
                    while len(pending) >= 320:  # 20 ms of native-endian slin
                        samples = array('h', pending[:320])
                        del pending[:320]
                        loud = sum(v*v for v in samples) >= 160 * 500**2
                        voiced = voiced + 1 if loud else 0
                        if voiced == 3:
                            return False
        return True

    def record(self, base, *, beep=False):
        start = time.monotonic()
        cue = ' BEEP' if beep else ''
        if self.command(f'RECORD FILE "{base}" wav "" 12000 0{cue} s=2') < 0:
            self.up()
            raise RuntimeError('recording failed')
        if time.monotonic() - start >= 12:
            raise RuntimeError('possibly truncated utterance')


def audio_command(args, data=None, *, capture=False):
    return subprocess.run(args, input=data or b'', stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          check=True, timeout=30).stdout


class LocalSpeech:
    def __init__(self):
        from vosk import Model  # optional dependency; no downloads or named models
        self.model = Model(model_path=os.environ['CIELINK_VOSK_MODEL'])
        self.piper = os.getenv('CIELINK_PIPER_MODEL')
        if self.piper and not Path(self.piper).is_file():
            raise ValueError('expected local Piper model')

    def synthesize(self, text, base):
        raw = base.with_name('tts.wav')
        data = (text + '\n').encode('utf-8')
        if self.piper:
            audio_command(['piper', '-m', self.piper, '-f', str(raw)], data)
        else:
            audio_command(['espeak-ng', '-b', '1', '-v', 'ru', '-w', str(raw), '--stdin'], data)
        audio_command(['sox', str(raw), '-r', '8000', '-c', '1', '-b', '16', '-e', 'signed-integer', str(base.with_suffix('.wav'))])

    def recognize(self, base):
        from vosk import KaldiRecognizer
        pcm = audio_command(['sox', str(base.with_suffix('.wav')), '-t', 'raw', '-L', '-r', '16000',
                             '-c', '1', '-b', '16', '-e', 'signed-integer', '-'], capture=True)
        recognizer = KaldiRecognizer(self.model, 16000)
        segments = []
        for offset in range(0, len(pcm), 8000):
            if recognizer.AcceptWaveform(pcm[offset:offset+8000]):
                segments.append(json.loads(recognizer.Result())['text'])
        segments.append(json.loads(recognizer.FinalResult())['text'])
        return ' '.join(filter(None, segments)).strip()


async def run_call(agi, speech, store, review, directory):
    rules = Consultation(FixtureQuoteAdapter(), 'sequential')
    dialogue = ConfirmedDialogue(rules, store)
    sid = review.start_session()
    state = SessionState(context=SessionContext(session_id=sid, channel='sip'))
    start = time.monotonic()

    def offset():
        return int((time.monotonic() - start) * 1000)

    def play(text, *, detect):
        review.append_event(sid, 'reply_generated', offset(), text)
        speech.synthesize(text, directory / 'prompt')
        try:
            complete = agi.play_prompt(directory / 'prompt', detect=detect)
        except Exception:
            review.append_event(sid, 'playback_interrupted', offset())
            raise
        if not complete:
            review.append_event(sid, 'playback_interrupted', offset())
        return complete

    outcome = 'engine_failure'
    offer = None
    reply = 'Здравствуйте. Это учебный помощник. Назовите адрес подключения.'
    try:
        for turn in range(21):
            terminal = bool(dialogue.receipt or dialogue.finished)
            complete = play(reply, detect=not terminal)
            if complete:
                dialogue.delivered()
                offer = state.active_offer.model_dump(mode='json') if state.active_offer else None
                end = offset()
                review.append_event(sid, 'offer_verified', end, json.dumps(offer, ensure_ascii=False))
                review.append_event(sid, 'playback_ended', end, reply)
                if terminal:
                    outcome = 'no_action'
                    return 'OPERATOR' if dialogue.receipt and dialogue.receipt.operation_type == 'handoff' else 'DONE'
            if turn == 20:
                break
            if not complete:
                cue = 'Я вас слышу. После сигнала повторите фразу с самого начала.'
                play(cue, detect=False)  # never delivers a pending proposal
                review.append_event(sid, 'playback_ended', offset(), cue)
            agi.record(directory / 'caller', beep=not complete)
            text = speech.recognize(directory / 'caller')
            agi.up()  # caller may have left while offline recognition ran
            if not text:
                raise RuntimeError('no recognized speech')
            inp = FinalInput(text=text, offset_ms=offset())
            state = handle_input(state, inp).state
            review.append_event(sid, 'final', inp.offset_ms, text)
            reply = await dialogue.respond(state, text)
        raise RuntimeError('turn limit')
    except Hangup:
        outcome = 'disconnected'
        raise
    finally:
        await rules.reads.close()
        receipt = asdict(dialogue.receipt) if dialogue.receipt else None
        review.finish_session(sid, receipt['operation_type'] if receipt else outcome, receipt=receipt,
                              offer=offer, coverage_status=rules.coverage_status)


def main():
    os.umask(0o077)
    try:
        agi = AGI(sys.stdin, sys.stdout)
        review = ReviewStore()
        store = OperationStore(os.environ['CIELINK_CRM_URL'])
        speech = LocalSpeech()
        # Use fixed /tmp, not AGI fields, caller IDs or arbitrary path fragments.
        with tempfile.TemporaryDirectory(prefix='cielink-', dir='/tmp') as tmp:
            result = asyncio.run(run_call(agi, speech, store, review, Path(tmp)))
        if agi.command('SET VARIABLE CIELINK_RESULT ' + result) != 1:
            raise RuntimeError('could not return outcome')
    except (Hangup, BrokenPipeError):
        return 0
    except Exception as exc:
        print('CIELINK AGI unavailable:', type(exc).__name__, file=sys.stderr)
        return 1  # dialplan defaults to the existing operator
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
