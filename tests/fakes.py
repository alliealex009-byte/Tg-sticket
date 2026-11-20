"""Minimal in-memory stand-ins for Motor collections and the Telegram Bot API."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


class DuplicateKeyError(Exception):
    pass


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _set(doc, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        doc = doc.setdefault(p, {})
    doc[parts[-1]] = value


def _cond(val, found, cond):
    if not isinstance(cond, dict) or not any(k.startswith("$") for k in cond):
        return found and val == cond
    for op, arg in cond.items():
        if op == "$ne" and found and val == arg:
            return False
        if op == "$in" and not (found and val in arg):
            return False
        if op == "$exists" and bool(arg) != found:
            return False
        if op in ("$lt", "$lte", "$gt", "$gte"):
            if not found or val is None:
                return False
            ok = {"$lt": val < arg, "$lte": val <= arg, "$gt": val > arg, "$gte": val >= arg}[op]
            if not ok:
                return False
    return True


def match(doc, flt):
    return all(_cond(*_get(doc, k), c) for k, c in (flt or {}).items())


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction == -1)
        return self

    def limit(self, n):
        self.docs = self.docs[:n] if n else self.docs
        return self

    async def to_list(self, n=None):
        return self.docs[:n] if n else self.docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class Collection:
    def __init__(self):
        self.docs = {}
        self.indexes = []
        self._auto = 0

    async def create_index(self, *a, **k):
        self.indexes.append((a, k))

    async def insert_one(self, doc):
        if "_id" not in doc:
            self._auto += 1
            doc["_id"] = f"auto{self._auto}"
        if doc["_id"] in self.docs:
            raise DuplicateKeyError(doc["_id"])
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, flt=None, proj=None):
        for d in self.docs.values():
            if match(d, flt):
                return copy.deepcopy(d)
        return None

    def find(self, flt=None, proj=None):
        return Cursor([copy.deepcopy(d) for d in self.docs.values() if match(d, flt)])

    async def count_documents(self, flt):
        return sum(1 for d in self.docs.values() if match(d, flt))

    def _apply(self, doc, upd, inserted):
        for k, v in upd.get("$set", {}).items():
            _set(doc, k, copy.deepcopy(v))
        if inserted:
            for k, v in upd.get("$setOnInsert", {}).items():
                _set(doc, k, copy.deepcopy(v))
        for k, v in upd.get("$inc", {}).items():
            cur, _ = _get(doc, k)
            _set(doc, k, (cur or 0) + v)

    async def _update(self, flt, upd, upsert, many=False):
        n = 0
        for d in list(self.docs.values()):
            if match(d, flt):
                self._apply(d, upd, False)
                n += 1
                if not many:
                    return SimpleNamespace(matched_count=1, modified_count=1, upserted=False)
        if n:
            return SimpleNamespace(matched_count=n, modified_count=n, upserted=False)
        if upsert:
            doc = {k: v for k, v in (flt or {}).items() if not isinstance(v, dict)}
            self._apply(doc, upd, True)
            self.docs[doc["_id"]] = doc
            return SimpleNamespace(matched_count=0, modified_count=0, upserted=True)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted=False)

    async def update_one(self, flt, upd, upsert=False):
        return await self._update(flt, upd, upsert)

    async def update_many(self, flt, upd, upsert=False):
        return await self._update(flt, upd, upsert, many=True)

    async def find_one_and_update(self, flt, upd, upsert=False, return_document=True):
        await self._update(flt, upd, upsert)
        return await self.find_one(flt)

    async def delete_many(self, flt):
        ids = [i for i, d in self.docs.items() if match(d, flt)]
        for i in ids:
            del self.docs[i]
        return SimpleNamespace(deleted_count=len(ids))


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = Collection()
        return self[key]


class FakeClient:
    def __init__(self):
        self.dbs = {}
        self.admin = SimpleNamespace(command=AsyncMock(return_value={"ok": 1}))
        self.close = MagicMock()

    def __getitem__(self, name):
        return self.dbs.setdefault(name, FakeDB())


class FakeBot:
    """Records Bot API sticker calls; ``fail`` is a list of exceptions to raise, in order."""

    def __init__(self):
        self.created, self.added, self.fail = [], [], []
        self.set_size = 5

    def _maybe_fail(self):
        if self.fail:
            raise self.fail.pop(0)

    async def create_new_sticker_set(self, user_id, name, title, stickers, sticker_type=None):
        self._maybe_fail()
        self.created.append(SimpleNamespace(user_id=user_id, name=name, title=title, stickers=stickers))
        return True

    async def get_sticker_set(self, name):
        from types import SimpleNamespace as NS
        if name == "missing":
            raise RuntimeError("STICKERSET_INVALID")
        items = [NS(file_id=f"fid{i}", emoji="😀", is_animated=(i == 0 and name == "anim"), is_video=False)
                 for i in range(self.set_size)]
        return NS(name=name, title="Food", sticker_type="regular", stickers=items)

    async def add_sticker_to_set(self, user_id, name, sticker):
        self._maybe_fail()
        self.added.append(SimpleNamespace(user_id=user_id, name=name, sticker=sticker))
        return True
