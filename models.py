from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

Base = declarative_base()

class CustomerInteraction(Base):
    __tablename__ = "customer_interactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # TODO: add tables for organizations, customers, agents and use IDs here instead of strings.
    organization = Column(String, nullable=False)
    customer = Column(String, nullable=False)
    agent = Column(String, nullable=False)
    audio_filename = Column(String, nullable=False)
    transcript_filename = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
