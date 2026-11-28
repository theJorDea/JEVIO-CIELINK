import sqlite3

from cielink.review import ReviewStore


def test_review_migrates_existing_sessions_and_displays_the_final_address(tmp_path):
    db = tmp_path / 'review.db'
    with sqlite3.connect(db) as conn:
        conn.execute('''CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT DEFAULT 'active',
            outcome TEXT, analysis_status TEXT DEFAULT 'pending', receipt TEXT, offer TEXT,
            coverage_status TEXT, findings TEXT DEFAULT '[]')''')
        conn.execute("INSERT INTO sessions (id,status) VALUES ('older','finished')")
    review = ReviewStore(db)
    assert review.detail('older')['address'] is None
    sid = review.start_session()
    address = 'улица Звёздная, дом 91, корпус 3'
    review.finish_session(sid, 'no_action', address=address, coverage_status='unavailable')
    saved = ReviewStore(db).detail(sid)
    assert saved['address'] == address
    assert saved['coverage_status'] == 'unavailable'
    assert saved['offer'] is None
    assert 'address' not in review.list_sessions()[0]
