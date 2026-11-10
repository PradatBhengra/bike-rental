def record(conn, actor_id: int | None, action: str, entity: str, entity_id: int | None, *, before: dict | None = None,
           after: dict | None = None, reason: str | None = None, ip: str | None = None) -> None:
    conn.execute(
        """INSERT INTO audit_log (actor_id, action, entity, entity_id, before, after, reason, ip)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (actor_id, action, entity, entity_id, before, after, reason, ip),
    )
