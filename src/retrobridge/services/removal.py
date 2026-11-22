from typing import Optional, Dict, Any, List
from retrobridge.storage.db import get_db_connection
from retrobridge.domain.systems import system_codes

def request_removal(title: str, system: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    actions_created = []
    with get_db_connection(db_path) as conn:
        cur = conn.cursor()
        query = "SELECT id, title, system, matched_file_path, matched_file_name FROM games WHERE title = ?"
        params = [title]
        if system:
            codes = system_codes(system)
            query += f" AND UPPER(system) IN ({','.join('?' * len(codes))})"
            params.extend(codes)
        cur.execute(query, params)
        games = cur.fetchall()

        for g in games:
            g_id = g["id"]
            # Move to removing
            conn.execute("UPDATE games SET state = 'removing' WHERE id = ?;", (g_id,))

            # 1. Unselect action in Transmission
            conn.execute("INSERT INTO pending_actions (game_id, action_type) VALUES (?, 'unselect');", (g_id,))
            # 2. Unlink action in Library-Dir
            conn.execute("INSERT INTO pending_actions (game_id, action_type) VALUES (?, 'unlink_library');", (g_id,))
            # 3. Delete action in Download-Dir
            conn.execute("INSERT INTO pending_actions (game_id, action_type) VALUES (?, 'delete_download');", (g_id,))

            actions_created.append({"game_id": g_id, "title": g["title"], "system": g["system"]})

        conn.commit()
    return actions_created
