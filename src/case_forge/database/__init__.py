from .base import DatabaseBackend, VersionConflictError
from .dynamodb import DynamoDBBackend
from .firestore import FirestoreBackend
from .memory import MemoryBackend

__all__ = ["BACKENDS", "DatabaseBackend", "VersionConflictError", "get_database"]

BACKENDS = {
    "dynamodb": DynamoDBBackend,
    "firestore": FirestoreBackend,
    "memory": MemoryBackend,
}


def get_database(name: str, **kwargs) -> DatabaseBackend:
    cls = BACKENDS.get(name)
    if not cls:
        raise ValueError(
            f"Unknown database backend: {name}. Available: {', '.join(BACKENDS.keys())}"
        )
    return cls(**kwargs)
