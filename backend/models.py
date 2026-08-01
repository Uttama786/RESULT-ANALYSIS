from sqlalchemy import Column, String, Integer, Float, LargeBinary
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    salt = Column(String, nullable=False)
    full_name = Column(String, default="")
    email = Column(String, default="")
    role = Column(String, default="student")
    department = Column(String, default="")
    created_at = Column(String, default="")

    def to_dict(self, include_sensitive: bool = False) -> dict:
        data = {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "email": self.email,
            "role": self.role,
            "department": self.department,
            "created_at": self.created_at
        }
        if include_sensitive:
            data["hashed_password"] = self.hashed_password
            data["salt"] = self.salt
        return data


class History(Base):
    __tablename__ = "history"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, nullable=False)
    username = Column(String, nullable=False)
    session_id = Column(String, default="")
    timestamp = Column(String, default="")
    usn_count = Column(Integer, default=0)
    usn_range_summary = Column(String, default="")
    excel_file = Column(String, default="")
    excel_path = Column(String, default="")
    excel_url = Column(String, default="")
    storage_provider = Column(String, default="db")
    excel_data = Column(LargeBinary, nullable=True)
    file_size_kb = Column(Float, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "usn_count": self.usn_count,
            "usn_range_summary": self.usn_range_summary,
            "excel_file": self.excel_file,
            "excel_path": self.excel_path,
            "excel_url": self.excel_url,
            "storage_provider": self.storage_provider,
            "has_persistent_storage": bool(self.excel_data is not None or self.excel_url),
            "file_size_kb": self.file_size_kb
        }
