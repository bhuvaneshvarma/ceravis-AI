from pydantic import BaseModel


class Zone(BaseModel):
    zone_id: str
    camera_id: str
    zone_name: str
    polygon: list[list[int]]