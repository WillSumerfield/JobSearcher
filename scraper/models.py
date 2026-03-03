from dataclasses import dataclass, field
from typing import Optional
import hashlib


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    date_posted: Optional[str] = None
    id: str = field(init=False)

    def __post_init__(self):
        raw = f"{self.title}{self.company}{self.url}".lower()
        self.id = hashlib.sha1(raw.encode()).hexdigest()[:16]

    def salary_display(self) -> str:
        if self.salary_min and self.salary_max:
            return f"£{int(self.salary_min):,}–£{int(self.salary_max):,}"
        if self.salary_min:
            return f"£{int(self.salary_min):,}+"
        if self.salary_max:
            return f"up to £{int(self.salary_max):,}"
        return "—"
