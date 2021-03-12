from datetime import datetime
from json import dumps
from json import loads
from os import makedirs
from os.path import dirname
from os.path import join
from re import sub
from sqlite3 import Connection
from sqlite3 import Cursor
from sqlite3 import connect
from typing import Dict
from typing import Generator
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

from filetype import guess_extension as filetype_guess_extension

from .__version__ import __version__
from .merge import merge_database
from .tables import journals_table
from .tables import list_columns
from .tables import make_journals_table
from .tables import make_settings_table
from .tables import make_submissions_table
from .tables import make_users_table
from .tables import settings_table
from .tables import submissions_table
from .tables import users_table
from .update import update_database

Key = Union[str, int, float]
Value = Union[str, int, float, None]
Entry = Dict[str, Union[List[str], Value]]


def guess_extension(file: bytes, default: str = "") -> str:
    if not file:
        return ""
    elif (file_type := filetype_guess_extension(file)) is None:
        return default
    elif (ext := str(file_type)) in (exts := ("zip", "octet-stream")):
        return default if default not in exts else ext
    else:
        return ext


def tiered_path(id_: Union[int, str], depth: int = 5, width: int = 2) -> str:
    assert isinstance(id_, int) or (isinstance(id_, str) and id_.isdigit()), "id not an integer"
    assert isinstance(depth, int) and depth > 0, "depth lower than 0"
    assert isinstance(width, int) and width > 0, "depth lower than 0"

    id_str: str = str(int(id_)).zfill(depth * width)
    return join(*[id_str[n:n + width] for n in range(0, depth * width, width)])


def clean_username(username: str) -> str:
    return str(sub(r"[^a-zA-Z0-9\-.~,]", "", username.lower().strip()))


class FADatabaseTable:
    def __init__(self, database: 'FADatabase', table: str):
        self.database: 'FADatabase' = database
        self.table: str = table
        self.columns_info_: List[Tuple[str, str]] = []
        self.columns_: List[str] = []
        self.column_id_: str = ""
        self.list_columns: List[str] = list_columns.get(table, [])

    def __len__(self) -> int:
        return self.database.connection.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def __getitem__(self, key: Union[Key, Entry]) -> Optional[Entry]:
        key = key if isinstance(key, dict) else {self.column_id: key}
        return entry[0] if (entry := list(self.cursor_to_dict(self.select(key)))) else None

    def __setitem__(self, key: Key, values: Entry):
        values = {k.upper(): self.format_list(v) if isinstance(v, list) else v for k, v in values.items()}
        values[self.column_id] = key
        self.insert(values)

    def __delitem__(self, key: Key):
        self.database.connection.execute(f"""DELETE FROM {self.table} WHERE {self.column_id} = ?""", (key,))

    def __contains__(self, key: Union[Key, Entry]) -> bool:
        return self[key] is not None

    def __iter__(self) -> Generator[Entry, None, None]:
        return self.cursor_to_dict(self.select())

    @property
    def columns_info(self) -> List[Tuple[str, str]]:
        self.columns_info_ = [
            info[1:]
            for info in self.database.connection.execute(f"pragma table_info({self.table})")
        ] if not self.columns_info_ else self.columns_info_
        return self.columns_info_

    @property
    def columns(self) -> List[str]:
        self.columns_ = [name for name, *_ in self.columns_info] if not self.columns_ else self.columns_
        return self.columns_

    @property
    def column_id(self) -> str:
        self.column_id_ = [
            name
            for name, *_, pk in self.columns_info if pk == 1
        ][0]
        return self.column_id_

    def add_to_list(self, key: Key, values: Dict[str, List[Value]]) -> bool:
        if not (values := {k.upper(): v for k, v in values.items() if v and k.upper() in self.list_columns}):
            return False
        elif not (item := self[key]):
            return False
        item = {k: item[k] for k in values.keys()}
        item_new = {k: sorted(set(item[k] + v), key=str.lower) for k, v in values.items()}
        self.update({k: self.format_list(v) for k, v in item_new.items()}, key) if item_new != item else None
        return item_new != item

    def remove_from_list(self, key: Key, values: Dict[str, List[Value]]) -> bool:
        if not (values := {k.upper(): v for k, v in values.items() if v and k.upper() in self.list_columns}):
            return False
        elif not (item := self[key]):
            return False
        item = {k: item[k] for k in values.keys()}
        item_new = {k: sorted(set(item[k]) - set(v), key=str.lower) for k, v in values.items()}
        self.update({k: self.format_list(v) for k, v in item_new.items()}, key) if item_new != item else None
        return item_new != item

    def reload(self):
        self.__init__(self.database, self.table)

    def cursor_to_dict(self, cursor: Cursor, columns: List[str] = None) -> Generator[Entry, None, None]:
        columns = map(str.upper, self.columns if columns is None else columns)
        return ({k: self.unpack_list(v) if k in self.list_columns else v for k, v in zip(columns, entry)}
                for entry in cursor)

    @staticmethod
    def format_list(obj: List[Value]) -> str:
        return "".join(f"|{e}|" for e in sorted(set(map(str, obj)), key=str.lower))

    @staticmethod
    def unpack_list(obj: str) -> List[str]:
        return [e for e in obj.split("|") if e]

    def format_dict(self, obj: Entry) -> Dict[str, Value]:
        obj = {k.upper().replace("_", ""): v for k, v in obj.items()}
        obj = {k: self.format_list(v) if isinstance(v := obj.get(k, ""), list) else v for k in self.columns}
        return obj

    def select(self, query: Dict[str, Union[List[Value], Value]] = None, columns: List[str] = None,
               query_and: bool = True, query_and_values: bool = False, like: bool = False,
               order: List[str] = None, limit: int = 0, offset: int = 0
               ) -> Cursor:
        query = {} if query is None else query
        query = {k: [v] if not isinstance(v, list) else v for k, v in query.items()}
        query = {k: vs for k, vs in query.items() if vs}
        order = [] if order is None else order
        columns = self.columns if columns is None else columns
        op: str = "like" if like else "="
        logic: str = "AND" if query_and else "OR"
        logic_values: str = "AND" if query_and_values else "OR"
        where_str: str = f" {logic} ".join(map(lambda q: f"({q})", [
            f" {logic_values} ".join([f"{k} {op} ?"] * len(vs))
            for k, vs in query.items()
        ]))
        order_str = ",".join(order)
        return self.database.connection.execute(
            f"""SELECT {','.join(columns)} FROM {self.table}
            {f' WHERE {where_str} ' if where_str else ''}
            {f' ORDER BY {order_str} ' if order_str else ''}
            {f' LIMIT {limit} ' if limit > 0 else ''}
            {f' OFFSET {offset} ' if limit > 0 and offset > 0 else ''}""",
            [v for values in query.values() for v in values]
        )

    def select_sql(self, where: str = "", values: List[Value] = None, columns: List[str] = None,
                   order: List[str] = None, limit: int = 0, offset: int = 0) -> Cursor:
        columns = self.columns if columns is None else columns
        order = [] if order is None else order
        return self.database.connection.execute(
            f"""SELECT {','.join(columns)} FROM {self.table}
            {f' WHERE {where} ' if where else ''}
            {f' ORDER BY {",".join(order)}' if order else ''}
            {f' LIMIT {limit}' if limit > 0 else ''}
            {f' OFFSET {offset}' if limit > 0 and offset > 0 else ''}""",
            [] if values is None else values
        )

    def insert(self, values: Dict[str, Value], replace: bool = True):
        self.database.connection.execute(
            f"""INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO {self.table}
            ({','.join(values.keys())}) VALUES ({','.join(['?'] * len(values))})""",
            [v for v in values.values()]
        )

    def update(self, values: Dict[str, Value], key: Optional[Key] = None):
        update_columns: List[str] = [f"{col} = ?" for col in values]
        where_str: str = f"WHERE {self.column_id} = ?" if key else ""
        self.database.connection.execute(
            f"UPDATE {self.table} SET {','.join(update_columns)} {where_str}",
            [v for v in values.values()] + ([key] if key is not None else [])
        )

    def delete(self, key: Key):
        del self[key]


class FADatabaseJournals(FADatabaseTable):
    def save_journal(self, journal: Dict[str, Union[int, str, list]]):
        journal = self.format_dict(journal)
        self[journal["ID"]] = journal

    def add_mention(self, journal_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.add_to_list(journal_id, {"MENTIONS": [user]})

    def remove_mention(self, journal_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.remove_from_list(journal_id, {"MENTIONS": [user]})


class FADatabaseSettings(FADatabaseTable):
    def __getitem__(self, setting: str) -> Optional[str]:
        return entry["SVALUE"] if (entry := super().__getitem__(setting)) is not None else None

    def __setitem__(self, setting: str, value: str):
        self.insert({"SETTING": setting, "SVALUE": value})

    def read_history(self) -> List[Tuple[float, str]]:
        return list(map(tuple, loads(self["HISTORY"])))

    def add_history(self, command: str, time: float = datetime.now().timestamp()):
        self["HISTORY"] = dumps(sorted([*self.read_history(), (time, command)], key=lambda h: h[0]))


class FADatabaseSubmissions(FADatabaseTable):
    def save_submission_file(self, submission_id: int, file: bytes, name: str, ext: str, guess_ext: bool = True) -> str:
        if not file:
            return ""

        ext = guess_extension(file, ext) if guess_ext else ext
        path = join(dirname(self.database.database_path), self.database.settings["FILESFOLDER"],
                    tiered_path(submission_id), name + f".{ext}" * bool(ext))

        makedirs(dirname(path), exist_ok=True)
        open(path, "wb").write(file)

        return ext

    def save_submission(self, submission: Dict[str, Union[int, str, list]], file: Optional[bytes] = None):
        submission = self.format_dict(submission)

        submission["FILEEXT"] = name.split(".")[-1] if "." in (name := submission["FILELINK"].split("/")[-1]) else ""
        submission["FILEEXT"] = self.save_submission_file(submission["ID"], file, "submission", submission["FILEEXT"])
        submission["FILESAVED"] = bool(file)

        self[submission["ID"]] = submission

    def set_folder(self, submission_id: int, folder: str) -> bool:
        folder_old: str = self[submission_id]["FOLDER"]
        self.update({"FOLDER": folder}, submission_id) if folder_old != (folder := folder.lower().strip()) else None
        return folder_old != folder

    def add_favorite(self, submission_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.add_to_list(submission_id, {"FAVORITE": [user]})

    def remove_favorite(self, submission_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.remove_from_list(submission_id, {"FAVORITE": [user]})

    def add_mention(self, submission_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.add_to_list(submission_id, {"MENTIONS": [user]})

    def remove_mention(self, submission_id: int, user: str) -> bool:
        user = clean_username(user)
        assert len(user) > 0, "User cannot be empty"
        return self.remove_from_list(submission_id, {"MENTIONS": [user]})


class FADatabaseUsers(FADatabaseTable):
    def new_user(self, user: str):
        user = clean_username(user)
        if user not in self:
            self[user] = {f: "" for f in self.columns}

    def activate_user(self, user: str):
        if (user_entry := self[(user := clean_username(user))]) is None:
            return
        self.update({"FOLDERS": self.format_list([f.strip("!") for f in user_entry["FOLDERS"]])}, user)

    def deactivate_user(self, user: str):
        if (user_entry := self[(user := clean_username(user))]) is None:
            return
        self.update({"FOLDERS": self.format_list([f"!{f.strip('!')}" for f in user_entry["FOLDERS"]])}, user)

    def add_user_folder(self, user: str, folder: str):
        if not (user_entry := self[(user := clean_username(user))]):
            return
        elif (folder := folder.lower()) in user_entry["FOLDERS"]:
            return
        elif f"!{folder}" in user_entry["FOLDERS"]:
            self.remove_user_folder(user, f"!{folder}")
        self.add_to_list(user, {"FOLDERS": [folder]})

    def remove_user_folder(self, user: str, folder: str):
        self.remove_from_list(clean_username(user), {"FOLDERS": [folder.lower()]})


class FADatabase:
    def __init__(self, database_path: str):
        self.database_path: str = database_path
        self.connection: Connection = connect(database_path)

        if journals_table not in (tables := self.tables):
            make_journals_table(self.connection)
        if settings_table not in tables:
            make_settings_table(self.connection)
        if submissions_table not in tables:
            make_submissions_table(self.connection)
        if users_table not in tables:
            make_users_table(self.connection)

        self.journals: FADatabaseJournals = FADatabaseJournals(self, journals_table)
        self.settings: FADatabaseSettings = FADatabaseSettings(self, settings_table)
        self.submissions: FADatabaseSubmissions = FADatabaseSubmissions(self, submissions_table)
        self.users: FADatabaseUsers = FADatabaseUsers(self, users_table)
        self.committed_changes: int = self.total_changes

    def __getitem__(self, table: str):
        return FADatabaseTable(self, table)

    def __iter__(self) -> Generator[Tuple[str, FADatabaseTable], None, None]:
        return ((table, FADatabaseTable(self, table)) for table in self.tables)

    def __contains__(self, table: str) -> bool:
        return table in self.tables

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def tables(self) -> List[str]:
        return [
            name
            for [name] in self.connection.execute(
                """SELECT name FROM sqlite_master 
                WHERE type = 'table' 
                AND name NOT LIKE 'sqlite_%'
                ORDER BY 1;"""
            )
        ]

    @property
    def version(self) -> str:
        return self.settings["VERSION"]

    @property
    def total_changes(self) -> int:
        return int(self.connection.total_changes)

    @property
    def is_clean(self) -> bool:
        return self.total_changes == self.committed_changes

    def upgrade(self):
        self.connection = update_database(self.connection, __version__)

        self.journals.reload()
        self.settings.reload()
        self.submissions.reload()
        self.users.reload()
        self.committed_changes: int = self.total_changes

    def commit(self):
        self.connection.commit()
        self.committed_changes = self.total_changes

    def rollback(self):
        self.connection.execute("ROLLBACK")

    def reset(self):
        self.close()
        self.__init__(self.database_path)

    def close(self):
        self.connection.close()

    def update(self, db_b: 'FADatabase'):
        merge_database(self.connection, dirname(self.database_path), db_b.connection, dirname(db_b.database_path))

    def vacuum(self):
        self.connection.execute("VACUUM")
