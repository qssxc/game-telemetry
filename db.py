import sqlite3

def connect(db_path: str, fast: bool = False, readonly=False):
    if readonly:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    if fast:
        # 적재는 언제든 처음부터 다시 돌릴 수 있다. 중간에 전원이 꺼져도
        # 원본 NDJSON 이 남아 있으므로 내구성을 거래해도 잃는 것이 없다.
        con.execute("PRAGMA synchronous = OFF")
        con.execute("PRAGMA cache_size = -64000")   # 64MB
    return con