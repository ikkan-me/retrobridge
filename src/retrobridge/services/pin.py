from typing import Optional
from retrobridge.storage.db import get_db_connection
from retrobridge.domain.systems import normalize_system

def set_game_pin(title: str, system: Optional[str] = None, pinned: bool = True, db_path: Optional[str] = None) -> bool:
    with get_db_connection(db_path) as conn:
        query = "UPDATE games SET is_pinned = ? WHERE title = ?"
        params = [1 if pinned else 0, title]
        if system:
            query += " AND UPPER(system) = ?"
            params.append(normalize_system(system))
        cur = conn.execute(query, params)
        conn.commit()
        return cur.rowcount > 0
