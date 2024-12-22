import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import (
    Base, Organization, OrganizationMember, User, Agent, CustomerInteraction, Category, CategoryScore,
    Standard, StandardComparison, Question, Answer, Summary, Sentiment, SilentPeriod, SpeakerMapping
)

# Create a session
load_dotenv()
AWS_DATABASE_USER = os.getenv("AWS_DATABASE_USER") # TODO: Change to secrets manager
AWS_DATABASE_PASS = os.getenv("AWS_DATABASE_PASS")
AWS_DATABASE_ENDPOINT = os.getenv("AWS_DATABASE_ENDPOINT")
AWS_DATABASE_PORT = os.getenv("AWS_DATABASE_PORT")
AWS_DATABASE_NAME = os.getenv("AWS_DATABASE_NAME")
DATABASE_URI = f"postgresql://{AWS_DATABASE_USER}:{AWS_DATABASE_PASS}@{AWS_DATABASE_ENDPOINT}:{AWS_DATABASE_PORT}/{AWS_DATABASE_NAME}"
engine = create_engine(DATABASE_URI)
Session = sessionmaker(bind=engine)
session = Session()
Base.metadata.drop_all(engine) # Drop tables if they exist
Base.metadata.create_all(engine) # Create tables if they don't exist

# Sample data
try:
    # Add organizations
    org1 = Organization(name="Netrio")
    org2 = Organization(name="Bergin Ventures")
    session.add_all([org1, org2])
    session.commit()

    # Add users
    user1 = User(name="Alice")
    user2 = User(name="Bob")
    user3 = User(name="Charlie")
    session.add_all([user1, user2, user3])
    session.commit()

    # Add organization members
    member1 = OrganizationMember(organization_id=org1.organization_id, user_id=user1.user_id, role="admin")
    member2 = OrganizationMember(organization_id=org1.organization_id, user_id=user2.user_id, role="member")
    member3 = OrganizationMember(organization_id=org2.organization_id, user_id=user3.user_id, role="admin")
    session.add_all([member1, member2, member3])
    session.commit()

    # Add agents
    agent1 = Agent(organization_id=org1.organization_id, name="Agent A")
    agent2 = Agent(organization_id=org2.organization_id, name="Agent B")
    session.add_all([agent1, agent2])
    session.commit()

    # Add customer interactions
    interaction1 = CustomerInteraction(
        organization_id=org1.organization_id,
        audio_filename="audio1.mp3",
        transcript_filename="transcript1.json",
        analysis_filename="analysis1.json",
        name="Interaction 1",
        agent_id=agent1.agent_id,
        status="COMPLETED"
    )
    session.add(interaction1)
    session.commit()

    # Add categories
    category1 = Category(organization_id=org1.organization_id, name="Category 1", description="Description 1")
    session.add(category1)
    session.commit()

    # Add category scores
    category_score1 = CategoryScore(category_id=category1.category_id, customer_interaction_id=interaction1.customer_interaction_id, score=8)
    session.add(category_score1)
    session.commit()

    # Add standards
    standard1 = Standard(organization_id=org1.organization_id, name="Receptionist", description="Expected behavior for receptionists")
    session.add(standard1)
    session.commit()

    # Add standard comparisons
    comparison1 = StandardComparison(standard_id=standard1.standard_id, customer_interaction_id=interaction1.customer_interaction_id, comparison="Matched")
    session.add(comparison1)
    session.commit()

    # Add questions
    question1 = Question(organization_id=org1.organization_id, question="What is the customer satisfaction level?")
    session.add(question1)
    session.commit()

    # Add answers
    answer1 = Answer(question_id=question1.question_id, customer_interaction_id=interaction1.customer_interaction_id, answer="High")
    session.add(answer1)
    session.commit()

    # Add summaries
    summary1 = Summary(customer_interaction_id=interaction1.customer_interaction_id, organization_id=org1.organization_id, summary="Customer was very satisfied.")
    session.add(summary1)
    session.commit()

    # Add sentiments
    sentiment1 = Sentiment(customer_interaction_id=interaction1.customer_interaction_id, organization_id=org1.organization_id, sentiment="Positive", segment_id=1)
    session.add(sentiment1)
    session.commit()

    # Add silent periods
    silent_period1 = SilentPeriod(customer_interaction_id=interaction1.customer_interaction_id, start_time_sec=15.0, end_time_sec=20.0)
    session.add(silent_period1)
    session.commit()

    # Add speaker mappings
    speaker_mapping1 = SpeakerMapping(customer_interaction_id=interaction1.customer_interaction_id, speaker_label="Speaker 1", role="Agent")
    speaker_mapping2 = SpeakerMapping(customer_interaction_id=interaction1.customer_interaction_id, speaker_label="Speaker 2", role="Customer")
    session.add_all([speaker_mapping1, speaker_mapping2])
    session.commit()

    print("Sample data loaded successfully!")

except Exception as e:
    session.rollback()
    print(f"An error occurred: {e}")

finally:
    session.close()