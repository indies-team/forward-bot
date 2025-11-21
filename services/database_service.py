from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL

Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class MessageMap(Base):
    __tablename__ = "message_map"

    slack_ts = Column(String, primary_key=True, nullable=True)   # Slack timestamp (ts)
    discord_id = Column(String, primary_key=True, nullable=True) # Discord message ID

def init_db():
    Base.metadata.create_all(bind=engine)

def save_mapping(slack_ts=None, discord_id=None):
    session = SessionLocal()
    mapping = MessageMap(slack_ts=slack_ts, discord_id=discord_id)
    session.merge(mapping)   # upsert
    session.commit()
    session.close()

def get_discord_id(slack_ts):
    session = SessionLocal()
    mapping = session.query(MessageMap).filter_by(slack_ts=slack_ts).first()
    session.close()
    return mapping.discord_id if mapping else None

def get_slack_ts(discord_id):
    session = SessionLocal()
    mapping = session.query(MessageMap).filter_by(discord_id=discord_id).first()
    session.close()
    return mapping.slack_ts if mapping else None

def delete_mapping_by_slack(slack_ts):
    session = SessionLocal()
    session.query(MessageMap).filter_by(slack_ts=slack_ts).delete()
    session.commit()
    session.close()

def delete_mapping_by_discord(discord_id):
    session = SessionLocal()
    session.query(MessageMap).filter_by(discord_id=discord_id).delete()
    session.commit()
    session.close()
