from sqlite3 import DatabaseError
from sqlite3 import OperationalError


class VersionMismatch(DatabaseError):
    pass


class MultipleConnections(OperationalError):
    pass
