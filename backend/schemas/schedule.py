"""日程相关的请求体（api.md【v4.0】/ D243、D244）。"""

from pydantic import BaseModel, Field


class ScheduleCreate(BaseModel):
    friend_id: str
    title: str = Field(min_length=1, max_length=1000)
    # date / time 都是**本地值**（D244）：date=YYYY-MM-DD，time=HH:MM（可空 = 只到日）
    date: str = Field(min_length=1)
    time: str | None = None
    nature: str | None = None  # start / due / appointment / remind
    advance_minutes: int | None = None
    owner: str | None = None  # user / friend


class ScheduleUpdate(BaseModel):
    title: str | None = None
    nature: str | None = None
    date: str | None = None
    time: str | None = None
    advance_minutes: int | None = None
    owner: str | None = None
    # 界面**只许**写 pending / settled；active / archived 归扫描器
    state: str | None = None
    settled_reason: str | None = None  # done / dropped
