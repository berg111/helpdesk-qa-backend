from sqlalchemy import (
    Column, Integer, String, ForeignKey, DateTime, Float, Text
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class Organization(Base):
    __tablename__ = 'organizations'
    organization_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class User(Base):
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class OrganizationMember(Base):
    __tablename__ = 'organization_members'
    organization_member_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    role = Column(String, nullable=False)  # "admin" or "member"


class Agent(Base):
    __tablename__ = 'agents'
    agent_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    name = Column(String, nullable=False)


class CustomerInteraction(Base):
    __tablename__ = 'customer_interactions'
    customer_interaction_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now())
    audio_filename = Column(String, nullable=False)
    transcript_filename = Column(String, nullable=False)
    analysis_filename = Column(String, nullable=False)
    name = Column(String, default="")
    agent_id = Column(Integer, ForeignKey('agents.agent_id'))
    status = Column(String, nullable=False)  # PENDING | IN PROGRESS | COMPLETED | FAILED


class Category(Base):
    __tablename__ = 'categories'
    category_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, default="")


class CategoryScore(Base):
    __tablename__ = 'category_scores'
    category_score_id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey('categories.category_id'), nullable=False)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    score = Column(Integer, nullable=False)


class Standard(Base):
    __tablename__ = 'standards'
    standard_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, default="")


class StandardComparison(Base):
    __tablename__ = 'standard_comparisons'
    standard_comparison_id = Column(Integer, primary_key=True)
    standard_id = Column(Integer, ForeignKey('standards.standard_id'), nullable=False)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    comparison = Column(Text, nullable=False)


class Question(Base):
    __tablename__ = 'questions'
    question_id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    question = Column(String, nullable=False)


class Answer(Base):
    __tablename__ = 'answers'
    answer_id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey('questions.question_id'), nullable=False)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    answer = Column(Text, nullable=False)


class Summary(Base):
    __tablename__ = 'summaries'
    summary_id = Column(Integer, primary_key=True)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    summary = Column(Text, nullable=False)


class Sentiment(Base):
    __tablename__ = 'sentiments'
    sentiment_id = Column(Integer, primary_key=True)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    organization_id = Column(Integer, ForeignKey('organizations.organization_id'), nullable=False)
    sentiment = Column(String, nullable=False)  # Positive | Neutral | Negative
    segment_id = Column(Integer, nullable=False)


class SilentPeriod(Base):
    __tablename__ = 'silent_periods'
    silent_period_id = Column(Integer, primary_key=True)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    start_time_sec = Column(Float, nullable=False)
    end_time_sec = Column(Float, nullable=False)


class SpeakerMapping(Base):
    __tablename__ = 'speaker_mappings'
    speaker_mapping_id = Column(Integer, primary_key=True)
    customer_interaction_id = Column(Integer, ForeignKey('customer_interactions.customer_interaction_id'), nullable=False)
    speaker_label = Column(String, nullable=False)
    role = Column(String, nullable=False)  # Agent | Customer