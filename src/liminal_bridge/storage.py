import json
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from abc import ABC, abstractmethod

# Use late import for CRDT to avoid circular dependency if needed, or import directly
from .crdt import CRDT, Register, PNCounter, GSet, ORSet, RevisionLog


def deserialize_crdt(data: Any) -> CRDT:
    """Helper to deserialize JSON data into a CRDT object."""
    if isinstance(data, dict):
        crdt_type = data.get("type")
        if crdt_type in ["register", "lww-register"]:
            return Register.from_dict(data)
        elif crdt_type == "pn-counter":
            return PNCounter.from_dict(data)
        elif crdt_type == "g-set":
            return GSet.from_dict(data)
        elif crdt_type == "or-set":
            return ORSet.from_dict(data)
        elif crdt_type == "revision-log":
            return RevisionLog.from_dict(data)
    # Default fallback for backwards compatibility
    return Register.from_dict(data)


class BaseStorageProvider(ABC):
    """
    Abstract base class for LiminalMesh storage backends.
    This provides an UeberDB-like abstraction layer.
    """

    @abstractmethod
    def init_db(self) -> None:
        """Initializes the database schema if necessary."""
        pass

    @abstractmethod
    def get_kv(self, key: str) -> Optional[CRDT]:
        """Retrieves a CRDT from the KV store."""
        pass

    @abstractmethod
    def get_all_kv(self) -> Dict[str, CRDT]:
        """Retrieves all CRDTs from the KV store."""
        pass

    @abstractmethod
    def save_kv(self, key: str, value: CRDT) -> None:
        """Persists a CRDT to the KV store."""
        pass

    @abstractmethod
    def get_metadata(self, key: str) -> Optional[Any]:
        """Retrieves arbitrary metadata (e.g., vector clock)."""
        pass

    @abstractmethod
    def save_metadata(self, key: str, value: Any) -> None:
        """Persists arbitrary metadata."""
        pass

    @abstractmethod
    def get_all_thoughts(self) -> Dict[str, Any]:
        """Retrieves all thoughts."""
        pass

    @abstractmethod
    def save_thought(self, node_id: str, content: Any) -> None:
        """Persists a thought."""
        pass


class SQLiteStorageProvider(BaseStorageProvider):
    def __init__(self, db_path: str = "liminal.db"):
        self.db_path = db_path
        self.conn = None

    def init_db(self) -> None:
        self.conn = sqlite3.connect(self.db_path)
        cursor = self.conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS thoughts (
                node_id TEXT PRIMARY KEY,
                content TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def get_kv(self, key: str) -> Optional[CRDT]:
        if not self.conn:
            return None
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            try:
                data = json.loads(row[0])
                return deserialize_crdt(data)
            except json.JSONDecodeError:
                pass
        return None

    def get_all_kv(self) -> Dict[str, CRDT]:
        result = {}
        if not self.conn:
            return result
        cursor = self.conn.cursor()
        cursor.execute("SELECT key, value FROM kv_store")
        for key, value_json in cursor.fetchall():
            try:
                data = json.loads(value_json)
                result[key] = deserialize_crdt(data)
            except json.JSONDecodeError:
                pass
        return result

    def save_kv(self, key: str, value: CRDT) -> None:
        if not self.conn:
            return
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, json.dumps(value.to_dict())),
        )
        self.conn.commit()

    def get_metadata(self, key: str) -> Optional[Any]:
        if not self.conn:
            return None
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return row[0]
        return None

    def save_metadata(self, key: str, value: Any) -> None:
        if not self.conn:
            return
        cursor = self.conn.cursor()
        value_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        cursor.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (key, value_str),
        )
        self.conn.commit()

    def get_all_thoughts(self) -> Dict[str, Any]:
        result = {}
        if not self.conn:
            return result
        cursor = self.conn.cursor()
        cursor.execute("SELECT node_id, content FROM thoughts")
        for node_id, content in cursor.fetchall():
            try:
                result[node_id] = json.loads(content)
            except json.JSONDecodeError:
                result[node_id] = {"content": content, "timestamp": 0}
        return result

    def save_thought(self, node_id: str, content: Any) -> None:
        if not self.conn:
            return
        cursor = self.conn.cursor()
        content_str = json.dumps(content) if isinstance(content, dict) else str(content)
        cursor.execute(
            "INSERT OR REPLACE INTO thoughts (node_id, content) VALUES (?, ?)",
            (node_id, content_str),
        )
        self.conn.commit()


class RedisStorageProvider(BaseStorageProvider):
    def __init__(self, url: str):
        try:
            import redis

            self.client = redis.Redis.from_url(url, decode_responses=True)
        except ImportError:
            raise ImportError(
                "RedisProvider requires the 'redis' package. Run: pip install redis"
            )

    def init_db(self) -> None:
        # Redis doesn't require schema initialization
        pass

    def get_kv(self, key: str) -> Optional[CRDT]:
        val = self.client.hget("liminal:kv_store", key)
        if val:
            try:
                return deserialize_crdt(json.loads(val))
            except json.JSONDecodeError:
                pass
        return None

    def get_all_kv(self) -> Dict[str, CRDT]:
        result = {}
        all_kv = self.client.hgetall("liminal:kv_store")
        for key, val in all_kv.items():
            try:
                result[key] = deserialize_crdt(json.loads(val))
            except json.JSONDecodeError:
                pass
        return result

    def save_kv(self, key: str, value: CRDT) -> None:
        self.client.hset("liminal:kv_store", key, json.dumps(value.to_dict()))

    def get_metadata(self, key: str) -> Optional[Any]:
        val = self.client.hget("liminal:metadata", key)
        if val:
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return None

    def save_metadata(self, key: str, value: Any) -> None:
        value_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.client.hset("liminal:metadata", key, value_str)

    def get_all_thoughts(self) -> Dict[str, Any]:
        result = {}
        all_thoughts = self.client.hgetall("liminal:thoughts")
        for node_id, content in all_thoughts.items():
            try:
                result[node_id] = json.loads(content)
            except json.JSONDecodeError:
                result[node_id] = {"content": content, "timestamp": 0}
        return result

    def save_thought(self, node_id: str, content: Any) -> None:
        content_str = json.dumps(content) if isinstance(content, dict) else str(content)
        self.client.hset("liminal:thoughts", node_id, content_str)


class PostgresStorageProvider(BaseStorageProvider):
    def __init__(self, url: str):
        try:
            import psycopg

            self.url = url
            self.conn = None
        except ImportError:
            raise ImportError(
                "PostgresStorageProvider requires the 'psycopg' package. Run: pip install psycopg"
            )

    def _get_conn(self):
        import psycopg

        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.url, autocommit=True)
        return self.conn

    def init_db(self) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS thoughts (
                    node_id TEXT PRIMARY KEY,
                    content TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

    def get_kv(self, key: str) -> Optional[CRDT]:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                try:
                    return deserialize_crdt(json.loads(row[0]))
                except json.JSONDecodeError:
                    pass
        return None

    def get_all_kv(self) -> Dict[str, CRDT]:
        result = {}
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM kv_store")
            for key, val in cur.fetchall():
                try:
                    result[key] = deserialize_crdt(json.loads(val))
                except json.JSONDecodeError:
                    pass
        return result

    def save_kv(self, key: str, value: CRDT) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv_store (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, json.dumps(value.to_dict())),
            )

    def get_metadata(self, key: str) -> Optional[Any]:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM metadata WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return row[0]
        return None

    def save_metadata(self, key: str, value: Any) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            value_str = (
                json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            )
            cur.execute(
                "INSERT INTO metadata (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value_str),
            )

    def get_all_thoughts(self) -> Dict[str, Any]:
        result = {}
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT node_id, content FROM thoughts")
            for node_id, content in cur.fetchall():
                try:
                    result[node_id] = json.loads(content)
                except json.JSONDecodeError:
                    result[node_id] = {"content": content, "timestamp": 0}
        return result

    def save_thought(self, node_id: str, content: Any) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            content_str = (
                json.dumps(content) if isinstance(content, dict) else str(content)
            )
            cur.execute(
                "INSERT INTO thoughts (node_id, content) VALUES (%s, %s) ON CONFLICT (node_id) DO UPDATE SET content = EXCLUDED.content",
                (node_id, content_str),
            )
