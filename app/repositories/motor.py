from __future__ import annotations

import logging

from app.repositories.base import GCSRepository
from app.schemas.motor import MotorRecord

logger = logging.getLogger(__name__)


def _motor_blob(user_id: str, motor_id: str) -> str:
    return f"users/{user_id}/motors/{motor_id}.json"


def _motors_prefix(user_id: str) -> str:
    return f"users/{user_id}/motors/"


class MotorRepository(GCSRepository):
    async def list(self, user_id: str) -> list[MotorRecord]:
        blob_names = await self._list(_motors_prefix(user_id))
        records: list[MotorRecord] = []
        for blob_name in blob_names:
            data = await self._read(blob_name)
            if data is None:
                continue
            try:
                records.append(MotorRecord.model_validate(data))
            except Exception:
                logger.warning("Skipping malformed motor record: %s", blob_name, exc_info=True)
                continue
        return records

    async def get(self, user_id: str, motor_id: str) -> MotorRecord | None:
        data = await self._read(_motor_blob(user_id, motor_id))
        if data is None:
            return None
        return MotorRecord.model_validate(data)

    async def save(self, user_id: str, motor_id: str, record: MotorRecord) -> None:
        await self._write(_motor_blob(user_id, motor_id), record.model_dump(mode="json"))

    async def delete(self, user_id: str, motor_id: str) -> None:
        await self._delete(_motor_blob(user_id, motor_id))

    async def list_all_users_with_motors(self) -> list[str]:
        all_blobs = await self._list("users/")
        users: set[str] = set()
        for blob_name in all_blobs:
            # Path: users/{user_id}/motors/{motor_id}.json
            parts = blob_name.split("/")
            if len(parts) >= 4 and parts[0] == "users" and parts[2] == "motors":
                users.add(parts[1])
        return sorted(users)

    async def delete_all_for_user(self, user_id: str) -> int:
        blobs = await self._list(_motors_prefix(user_id))
        count = sum(1 for b in blobs if b.endswith(".json"))
        await self._delete(_motors_prefix(user_id))
        return count
