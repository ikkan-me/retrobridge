from typing import Optional
from retrobridge.storage.db import get_db_connection
from retrobridge.domain.systems import system_codes

def set_game_pin(title: str, system: Optional[str] = None, pinned: bool = True, db_path: Optional[str] = None) -> bool:
    with get_db_connection(db_path) as conn:
        query = "UPDATE games SET is_pinned = ? WHERE title = ?"
        params = [1 if pinned else 0, title]
        if system:
            codes = system_codes(system)
            query += f" AND UPPER(system) IN ({','.join('?' * len(codes))})"
            params.extend(codes)
        cur = conn.execute(query, params)
        conn.commit()
        return cur.rowcount > 0
