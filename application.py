import os
from flask import Flask, request, jsonify, make_response, send_file
import io
from flask_cors import CORS
from dotenv import load_dotenv
import json
import boto3
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import NoResultFound
from models import (
    Base, Organization, OrganizationMember, User, Agent, CustomerInteraction, Category, CategoryScore,
    Standard, StandardComparison, Question, Answer, Summary, Sentiment, SilentPeriod, SpeakerMapping,
    ReviewFlags, Configuration, ConfigurationCategory, ConfigurationQuestion, ConfigurationStandard
)
import uuid
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity, 
    verify_jwt_in_request, set_access_cookies, unset_jwt_cookies
)
from datetime import datetime, timedelta

load_dotenv()

FRONTEND_URL = os.getenv("FRONTEND_URL")

application = Flask(__name__)

# JWT and Auth
bcrypt = Bcrypt(application)
application.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
application.config["JWT_TOKEN_LOCATION"] = ["cookies"]   # Use cookies for token storage
application.config["JWT_ACCESS_COOKIE_NAME"] = "access_token"
application.config["JWT_COOKIE_CSRF_PROTECT"] = False
jwt = JWTManager(application)

# CORS
CORS(application, 
     supports_credentials=True, 
     origins=[FRONTEND_URL], 
     resources={r"/*": {"origins": FRONTEND_URL}}
)

# Configure AWS Resources
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = "us-west-2"
# s3
S3_AUDIO_BUCKET_NAME = "customer-service-qa-audio"
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)
# AWS Transcribe
S3_TRANSCRIPT_BUCKET_NAME = "customer-service-qa-transcripts"
transcribe_client = boto3.client(
    "transcribe",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)
# AWS SQS
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL")
sqs = boto3.client(
    "sqs",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)
# AWS RDS
AWS_DATABASE_USER = os.getenv("AWS_DATABASE_USER")
AWS_DATABASE_PASS = os.getenv("AWS_DATABASE_PASS")
AWS_DATABASE_ENDPOINT = os.getenv("AWS_DATABASE_ENDPOINT")
AWS_DATABASE_PORT = os.getenv("AWS_DATABASE_PORT")
AWS_DATABASE_NAME = os.getenv("AWS_DATABASE_NAME")
DATABASE_URI = f"postgresql://{AWS_DATABASE_USER}:{AWS_DATABASE_PASS}@{AWS_DATABASE_ENDPOINT}:{AWS_DATABASE_PORT}/{AWS_DATABASE_NAME}"
engine = create_engine(DATABASE_URI)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine) # Create tables if they don't exist



################# HELPER FUNCTIONS #################

def generate_unique_filename(original_filename):
    unique_id = str(uuid.uuid4())
    extension = original_filename.rsplit('.', 1)[-1]  # Get file extension
    secure_name = secure_filename(original_filename.rsplit('.', 1)[0])  # Sanitize filename
    return f"{secure_name}_{unique_id}.{extension}"

def upload_audio_to_s3(file, filename):
    '''
    Upload a given file to s3.
    '''
    unique_filename = generate_unique_filename(file.filename)
    unique_filename = filename
    # Upload file to S3
    try:
        print("Starting upload for:", filename)
        s3_client.upload_fileobj(file, S3_AUDIO_BUCKET_NAME, unique_filename)
        print("Finished upload for:", filename)
    except ClientError as e:
        raise e
    return unique_filename

def create_customer_interaction(audio_filename, transcript_filename, organization_id, agent_id, name=''):
    '''
    Create a new entry in the customer_interactions table in the DB and set processing 
    status to "PENDING".
    '''
    # Create a session
    session = Session()

    try:
        # Create a new CustomerInteraction instance
        new_interaction = CustomerInteraction(
            audio_filename=audio_filename,
            transcript_filename=transcript_filename,
            analysis_filename="PENDING",
            organization_id=organization_id,
            agent_id=agent_id,
            name=name,
            status="PENDING"  # Set initial status to PENDING
        )

        # Add the instance to the session
        session.add(new_interaction)

        # Commit the session to save changes
        session.commit()
        print(f"Customer interaction created with ID: {new_interaction.customer_interaction_id}")

        # Return the ID of the created interaction
        return new_interaction.customer_interaction_id

    except Exception as e:
        # Rollback in case of error
        session.rollback()
        print(f"An error occurred: {e}")

    finally:
        # Close the session
        session.close()

def start_transcription_job(filename):
    s3_audio_file_path = f"s3://{S3_AUDIO_BUCKET_NAME}/{filename}"
    try:
        # print(f"Starting Job {filename}")
        transcribe_client.start_transcription_job(
            TranscriptionJobName=filename,
            Media={"MediaFileUri": s3_audio_file_path},
            MediaFormat='mp3',
            LanguageCode="en-US",
            OutputBucketName=S3_TRANSCRIPT_BUCKET_NAME,
            Settings={
                'ShowSpeakerLabels': True,
                'MaxSpeakerLabels': 2
            }
        )
    except Exception as e:
        print(filename, "failed to start transaction job:", e)

def process_file_worker(audio_file, unique_filename, organization_id, agent_id, 
                              category_ids, standard_id, question_ids, new_interaction_ids):
    
    _ = upload_audio_to_s3(audio_file, unique_filename)
    _ = start_transcription_job(unique_filename)
    customer_interaction_id = create_customer_interaction(unique_filename, unique_filename+".json", organization_id, agent_id)
    new_interaction_ids.append(customer_interaction_id)
    # Send a job to SQS
    sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps({
            "customer_interaction_id": customer_interaction_id,
            "category_ids": category_ids,
            "standard_id": standard_id,
            "question_ids": question_ids,
            "organization_id": organization_id
        })
    )
    return

def get_current_user():
    '''
    returns a dictionary {"user_id": users id, "email": users email}
    '''
    try:
        current_user = get_jwt_identity()
        return json.loads(current_user)
    except Exception as e:
        print(f"Error: {e}")  # Log errors for debugging
        return jsonify({"error": "Unauthorized"}), 401

def authenticate_org_member(organization_id, user_id):
    # TODO
    return True

################# APPLICATION ENDPOINTS #################

@application.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = FRONTEND_URL  # Frontend origin
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response

@application.route('/')
def index():
    return "index"

@application.route('/register', methods=['POST'])
def register():
    data = request.json
    hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
    email = data['email']
    name = email.split("@")[0]
    new_user = User(name=name, email=email, hashed_password=hashed_password)
    
    try:
        with Session() as db_session:
            db_session.add(new_user)
            db_session.commit()
            return jsonify({"message": "User registered successfully"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@application.route('/login', methods=['POST'])
def login():
    with Session() as db_session:
        data = request.json
        user = db_session.query(User).filter_by(email=data['email']).first()

        if user and bcrypt.check_password_hash(user.hashed_password, data['password']):
            identity = {
                "user_id": user.user_id,
                "email": user.email
            }
            access_token = create_access_token(identity=json.dumps(identity))
            response = make_response(jsonify({"message": "Login successful"}))
            response.set_cookie(
                "access_token",
                access_token,
                httponly=True,
                secure=True,
                samesite='None',
                max_age=60*60,  # Set lifespan to 1 hour
                path='/'
            )
            return response
        return jsonify({"error": "Invalid credentials"}), 401

@application.route('/logout', methods=['POST'])
@jwt_required()
def logout():
    response = make_response(jsonify({"message": "Login successful"}))
    response.set_cookie(
        "access_token",
        "", # Empty (to delete basically)
        httponly=True,
        secure=True,
        samesite='None',
        max_age=0,
        path='/'
    )
    return response

@application.route('/whoami', methods=['GET'])
@jwt_required()
def whoami():
    try:
        current_user = get_current_user()
        print(type(current_user), current_user['user_id'], current_user['email'])
        return jsonify({"logged_in_as": current_user}), 200
    except Exception as e:
        print(f"Error: {e}")  # Log errors for debugging
        return jsonify({"error": "Unauthorized"}), 401


@application.route('/upload-and-analyze', methods=['POST'])
@jwt_required()
def upload_and_analyze():
    '''
    Sends a job to worker
    '''
    current_user = get_current_user()
    user_id = current_user['user_id']
    # Get audio files from request
    if 'audio_files' not in request.files:
        return jsonify({"error": "No audio files provided"}), 400
    files = request.files.getlist('audio_files')
    if not files or any(file.filename == '' for file in files):
        return jsonify({"error": "One or more files are missing filenames"}), 400
    
    organization_id = int(request.form.get('organization_id')) # the organization uploading the audio
    if not authenticate_org_member(organization_id, user_id):
        return jsonify({"error": "Unauthorized"}), 401
    agent_id = int(request.form.get('agent_id')) # the agent heard in the audio
    standard_id = int(request.form.get('standard_id')) # the standard to compare to
    # Scoring categories
    category_ids_str = request.form.get('category_ids')
    if category_ids_str:
        category_ids_str = category_ids_str.split(',')
        category_ids = [int(c) for c in category_ids_str]
    else:
        print("No scoring categories selected.")
        category_ids = []
    # Questions
    question_ids_str = request.form.get('question_ids')
    if question_ids_str:
        question_ids_str = question_ids_str.split(',')
        question_ids = [int(q) for q in question_ids_str]
    else:
        print("No questions selected.")
        question_ids = []

    new_interaction_ids = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for file in files:
            unique_filename = generate_unique_filename(file.filename)
            executor.submit(process_file_worker, file, unique_filename,
                            organization_id, agent_id, category_ids, standard_id, question_ids, new_interaction_ids)

    return jsonify(new_interaction_ids), 200


# Getters / Setters for interacting with the DB

@application.route('/interactions/<int:interaction_id>', methods=['GET'])
@jwt_required()
def get_interaction(interaction_id):
    """
    Fetch a customer interaction by its ID.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Query the database for the interaction
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id
        ).one()

        # Check if the user is authorized to access this interaction
        if not authenticate_org_member(interaction.organization_id, user_id):
            return jsonify({"error": "Unauthorized. You don't have access to this organization."}), 401

        # Serialize the interaction to JSON
        interaction_data = {
            "customer_interaction_id": interaction.customer_interaction_id,
            "organization_id": interaction.organization_id,
            "audio_filename": interaction.audio_filename,
            "transcript_filename": interaction.transcript_filename,
            "analysis_filename": interaction.analysis_filename,
            "agent_id": interaction.agent_id,
            "name": interaction.name,
            "status": interaction.status,
            "created_at": interaction.created_at.isoformat()  # Convert datetime to string
        }

        return jsonify(interaction_data), 200

    except NoResultFound:
        return jsonify({"error": "Interaction not found"}), 404

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching interaction: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions', methods=['GET'])
@jwt_required()
def get_interactions_by_organization(organization_id):
    """
    Fetch all customer interactions for a specific organization ID with pagination.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Get pagination parameters from the request
        per_page = request.args.get('per_page', type=int, default=10)
        page = request.args.get('page', type=int, default=1)

        # Validate pagination parameters
        if page < 1 or per_page < 1:
            return jsonify({"error": "Invalid pagination parameters. Page and per_page must be greater than 0."}), 400

        # Query the database for interactions belonging to the organization
        query = session.query(CustomerInteraction).filter_by(organization_id=organization_id)

        # Pagination logic
        total_count = query.count()
        interactions = query.offset((page - 1) * per_page).limit(per_page).all()

        # Serialize interactions to JSON
        interactions_data = [
            {
                "customer_interaction_id": interaction.customer_interaction_id,
                "organization_id": interaction.organization_id,
                "audio_filename": interaction.audio_filename,
                "transcript_filename": interaction.transcript_filename,
                "analysis_filename": interaction.analysis_filename,
                "agent_id": interaction.agent_id,
                "name": interaction.name,
                "status": interaction.status,
                "created_at": interaction.created_at.isoformat(),  # Convert datetime to string
            }
            for interaction in interactions
        ]

        return jsonify({
            "total_count": total_count,
            "page": page,
            "per_page": per_page,
            "results": interactions_data
        }), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching interactions: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()



@application.route('/organizations/<int:organization_id>/interactions/filter', methods=['GET'])
@jwt_required()
def filter_customer_interactions(organization_id):
    """
    Filter customer interactions based on multiple criteria, including min and max scores for categories.
    
    request:
        - agents (list of integers): Filter by agent IDs.
        - category_scores (JSON string): Example: {"1": 8, "2": 5} (filter interactions where category 1 scored >= 8 and category 2 scored >= 5).
        - question_answers (JSON string): Example: {"3": "Yes", "4": "No"} (filter interactions where question 3 has "Yes" and question 4 has "No").
        - standards (list of integers): Filter by standard IDs.
        - needs_review (boolean): Filter interactions flagged for review.
        - per_page (integer): Number of results per page (default: 10).
        - page (integer): Page number (default: 1)

    example response:
    {
        "total_count": 42,
        "page": 1,
        "per_page": 10,
        "results": [
            {
            "interaction_id": 1,
            "agent_id": 3,
            "status": "COMPLETED",
            "created_at": "2024-12-23T12:00:00Z"
            },
            {
            "interaction_id": 2,
            "agent_id": 5,
            "status": "COMPLETED",
            "created_at": "2024-12-23T12:05:00Z"
            }
        ]
    }

    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Get filter parameters from the request
        agent_ids = request.args.getlist('agents')  # List of agent IDs
        category_scores = request.args.get('category_scores', type=str)  # JSON string: {category_id: {"min": x, "max": y}}
        question_answers = request.args.get('question_answers', type=str)  # JSON string: {question_id: answer}
        standard_ids = request.args.getlist('standards')  # List of standard IDs
        needs_review = request.args.get('needs_review', type=bool)  # Flag for review
        per_page = request.args.get('per_page', type=int, default=10)
        page = request.args.get('page', type=int, default=1)

        # Build the query
        query = session.query(CustomerInteraction).filter_by(organization_id=organization_id)

        # Apply agent filter
        if agent_ids:
            query = query.filter(CustomerInteraction.agent_id.in_(agent_ids))

        # Apply needs review filter
        if needs_review:
            query = query.join(ReviewFlags).filter(ReviewFlags.review_flag == True)

        # Apply standards filter
        if standard_ids:
            query = query.join(StandardComparison).filter(StandardComparison.standard_id.in_(standard_ids))

        # Apply question and answer filter
        if question_answers:
            question_answers = json.loads(question_answers)  # Parse JSON string
            for question_id, answer in question_answers.items():
                query = query.join(Answer).filter(
                    Answer.question_id == question_id,
                    Answer.answer == answer
                )

        # Apply category and score filter (min and max)
        if category_scores:
            category_scores = json.loads(category_scores)  # Parse JSON string
            for category_id, score_range in category_scores.items():
                min_score = score_range.get("min", 0)  # Default min: 0
                max_score = score_range.get("max", 10)  # Default max: 10
                query = query.join(CategoryScore).filter(
                    CategoryScore.category_id == category_id,
                    CategoryScore.score >= min_score,
                    CategoryScore.score <= max_score
                )

        # Pagination
        total_count = query.count()
        interactions = query.offset((page - 1) * per_page).limit(per_page).all()

        # Serialize results
        results = [
            {
                "interaction_id": interaction.customer_interaction_id,
                "agent_id": interaction.agent_id,
                "status": interaction.status,
                "created_at": interaction.created_at.isoformat(),
                # Add more fields as needed
            }
            for interaction in interactions
        ]

        return jsonify({
            "total_count": total_count,
            "page": page,
            "per_page": per_page,
            "results": results
        }), 200

    except Exception as e:
        application.logger.error(f"Error filtering customer interactions: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/questions-answers', methods=['GET'])
@jwt_required()
def get_questions_and_answers(organization_id, interaction_id):
    """
    Fetch all questions and their corresponding answers for a specific customer interaction.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Check if the interaction exists
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not interaction:
            return jsonify({"error": "Interaction not found"}), 404

        # Fetch the questions and answers associated with the interaction
        questions_answers = session.query(Question, Answer).join(Answer).filter(
            Answer.customer_interaction_id == interaction_id,
            Question.organization_id == organization_id
        ).all()

        # Serialize the results
        questions_answers_data = [
            {
                "question_id": question.question_id,
                "question": question.question,
                "answer_id": answer.answer_id,
                "answer": answer.answer
            }
            for question, answer in questions_answers
        ]

        return jsonify(questions_answers_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching questions and answers: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/category-scores', methods=['GET'])
@jwt_required()
def get_category_scores(organization_id, interaction_id):
    """
    Fetch all category scores for a specific customer interaction.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Check if the interaction exists
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not interaction:
            return jsonify({"error": "Interaction not found"}), 404

        # Fetch the category scores for the interaction
        category_scores = session.query(Category, CategoryScore).join(CategoryScore).filter(
            CategoryScore.customer_interaction_id == interaction_id,
            Category.organization_id == organization_id
        ).all()

        # Serialize the results
        category_scores_data = [
            {
                "category_id": category.category_id,
                "category_name": category.name,
                "score": category_score.score
            }
            for category, category_score in category_scores
        ]

        return jsonify(category_scores_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching category scores: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/summary', methods=['GET'])
@jwt_required()
def get_interaction_summary(organization_id, interaction_id):
    """
    Fetch the summary for a specific customer interaction.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Check if the interaction exists
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not interaction:
            return jsonify({"error": "Interaction not found"}), 404

        # Fetch the summary for the interaction
        summary = session.query(Summary).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not summary:
            return jsonify({"error": "Summary not found"}), 404

        # Serialize the summary
        summary_data = {
            "summary_id": summary.summary_id,
            "interaction_id": summary.customer_interaction_id,
            "organization_id": summary.organization_id,
            "summary_text": summary.summary
        }

        return jsonify(summary_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching interaction summary: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/standard-comparison', methods=['GET'])
@jwt_required()
def get_standard_comparison(organization_id, interaction_id):
    """
    Fetch the standard comparison for a specific customer interaction.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Check if the interaction exists
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not interaction:
            return jsonify({"error": "Interaction not found"}), 404

        # Fetch the standard comparison for the interaction
        standard_comparison = session.query(StandardComparison).filter_by(
            customer_interaction_id=interaction_id
        ).one_or_none()

        if not standard_comparison:
            return jsonify({"error": "Standard comparison not found"}), 404

        # Serialize the standard comparison
        comparison_data = {
            "standard_comparison_id": standard_comparison.standard_comparison_id,
            "interaction_id": standard_comparison.customer_interaction_id,
            "standard_id": standard_comparison.standard_id,
            "comparison_text": standard_comparison.comparison
        }

        return jsonify(comparison_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching standard comparison: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()




@application.route('/organizations/<int:organization_id>/interactions/flagged', methods=['GET'])
@jwt_required()
def get_flagged_interactions_by_organization(organization_id):
    """
    Fetch all customer interactions flagged for review for a specific organization ID.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for flagged interactions belonging to the organization
        flagged_interactions = (
            session.query(CustomerInteraction)
            .join(ReviewFlags, CustomerInteraction.customer_interaction_id == ReviewFlags.customer_interaction_id)
            .filter(
                CustomerInteraction.organization_id == organization_id,
                ReviewFlags.review_flag == True  # Only flagged interactions
            )
            .all()
        )

        # Serialize flagged interactions to JSON
        flagged_interactions_data = [
            {
                "customer_interaction_id": interaction.customer_interaction_id,
                "organization_id": interaction.organization_id,
                "audio_filename": interaction.audio_filename,
                "transcript_filename": interaction.transcript_filename,
                "analysis_filename": interaction.analysis_filename,
                "agent_id": interaction.agent_id,
                "name": interaction.name,
                "status": interaction.status,
                "created_at": interaction.created_at.isoformat(),  # Convert datetime to string
                "flag_reason": flag.reason,
                "was_reviewed": flag.was_reviewed
            }
            for interaction, flag in zip(
                flagged_interactions,
                [
                    session.query(ReviewFlags)
                    .filter_by(customer_interaction_id=interaction.customer_interaction_id)
                    .one()
                    for interaction in flagged_interactions
                ]
            )
        ]

        return jsonify(flagged_interactions_data), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching flagged interactions: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/recent/<int:minutes>', methods=['GET'])
@jwt_required()
def get_recent_interactions_by_organization(organization_id, minutes):
    """
    Fetch all customer interactions for a specific organization in the last `x` minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Calculate the time threshold
        time_threshold = datetime.now() - timedelta(minutes=minutes)

        # Query the database for recent interactions
        recent_interactions = (
            session.query(CustomerInteraction)
            .filter(
                CustomerInteraction.organization_id == organization_id,
                CustomerInteraction.created_at >= time_threshold
            )
            .all()
        )

        # Serialize the interactions to JSON
        recent_interactions_data = [
            {
                "customer_interaction_id": interaction.customer_interaction_id,
                "organization_id": interaction.organization_id,
                "audio_filename": interaction.audio_filename,
                "transcript_filename": interaction.transcript_filename,
                "analysis_filename": interaction.analysis_filename,
                "agent_id": interaction.agent_id,
                "name": interaction.name,
                "status": interaction.status,
                "created_at": interaction.created_at.isoformat()  # Convert datetime to string
            }
            for interaction in recent_interactions
        ]

        return jsonify(recent_interactions_data), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching recent interactions: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/questions', methods=['GET'])
@jwt_required()
def get_questions_by_organization(organization_id):
    """
    Fetch all questions for a specific organization ID.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for questions belonging to the organization
        questions = session.query(Question).filter_by(organization_id=organization_id).all()

        # Serialize questions to JSON
        questions_data = [
            {
                "question_id": question.question_id,
                "organization_id": question.organization_id,
                "question": question.question
            }
            for question in questions
        ]

        return jsonify(questions_data), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching questions: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/categories', methods=['GET'])
@jwt_required()
def get_categories_by_organization(organization_id):
    """
    Fetch all categories for a specific organization ID.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for categories belonging to the organization
        categories = session.query(Category).filter_by(organization_id=organization_id).all()

        # Serialize categories to JSON
        categories_data = [
            {
                "category_id": category.category_id,
                "organization_id": category.organization_id,
                "name": category.name,
                "description": category.description
            }
            for category in categories
        ]

        return jsonify(categories_data), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching categories: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/standards', methods=['GET'])
@jwt_required()
def get_standards_by_organization(organization_id):
    """
    Fetch all standards for a specific organization ID.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for standards belonging to the organization
        standards = session.query(Standard).filter_by(organization_id=organization_id).all()

        # Serialize standards to JSON
        standards_data = [
            {
                "standard_id": standard.standard_id,
                "organization_id": standard.organization_id,
                "name": standard.name,
                "description": standard.description
            }
            for standard in standards
        ]

        return jsonify(standards_data), 200

    except Exception as e:
        # Log the exception for debugging
        application.logger.error(f"Error fetching standards: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/categories', methods=['POST'])
@jwt_required()
def create_category(organization_id):
    """
    Create a new category for a specific organization.

    request:
        Content-Type: application/json

        {
            "name": "Clarity",
            "description": "Measures the clarity of communication in interactions."
        }

    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Parse the request data
        data = request.get_json()
        name = data.get("name")
        description = data.get("description", "")

        if not name:
            return jsonify({"error": "Name is required"}), 400

        # Create and save the new category
        new_category = Category(
            organization_id=organization_id,
            name=name,
            description=description
        )
        session.add(new_category)
        session.commit()

        return jsonify({
            "message": "Category created successfully",
            "category_id": new_category.category_id
        }), 201

    except Exception as e:
        application.logger.error(f"Error creating category: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/questions', methods=['POST'])
@jwt_required()
def create_question(organization_id):
    """
    Create a new question for a specific organization.

    request:
        Content-Type: application/json

        {
            "question": "Did the agent greet the customer?"
        }
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Parse the request data
        data = request.get_json()
        question_text = data.get("question")

        if not question_text:
            return jsonify({"error": "Question text is required"}), 400

        # Create and save the new question
        new_question = Question(
            organization_id=organization_id,
            question=question_text
        )
        session.add(new_question)
        session.commit()

        return jsonify({
            "message": "Question created successfully",
            "question_id": new_question.question_id
        }), 201

    except Exception as e:
        application.logger.error(f"Error creating question: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/standards', methods=['POST'])
@jwt_required()
def create_standard(organization_id):
    """
    Create a new standard for a specific organization.

    request:
        Content-Type: application/json

        {
            "name": "Politeness Standard",
            "description": "Defines the expected politeness level during interactions."
        }
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Parse the request data
        data = request.get_json()
        name = data.get("name")
        description = data.get("description", "")

        if not name:
            return jsonify({"error": "Name is required"}), 400

        # Create and save the new standard
        new_standard = Standard(
            organization_id=organization_id,
            name=name,
            description=description
        )
        session.add(new_standard)
        session.commit()

        return jsonify({
            "message": "Standard created successfully",
            "standard_id": new_standard.standard_id
        }), 201

    except Exception as e:
        application.logger.error(f"Error creating standard: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/agents', methods=['GET'])
@jwt_required()
def get_agents_by_organization(organization_id):
    """
    Fetch all agents for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for agents belonging to the organization
        agents = session.query(Agent).filter_by(organization_id=organization_id).all()

        # Serialize agents to JSON
        agents_data = [
            {
                "agent_id": agent.agent_id,
                "organization_id": agent.organization_id,
                "name": agent.name
            }
            for agent in agents
        ]

        return jsonify(agents_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching agents: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/agents', methods=['POST'])
@jwt_required()
def create_agent(organization_id):
    """
    Create a new agent for a specific organization.

    request:
    {
        "name": "Agent John Doe"
    }
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Parse the request data
        data = request.get_json()
        name = data.get("name")

        if not name:
            return jsonify({"error": "Agent name is required"}), 400

        # Create and save the new agent
        new_agent = Agent(
            organization_id=organization_id,
            name=name
        )
        session.add(new_agent)
        session.commit()

        return jsonify({
            "message": "Agent created successfully",
            "agent_id": new_agent.agent_id
        }), 201

    except Exception as e:
        application.logger.error(f"Error creating agent: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


from datetime import datetime, timedelta

@application.route('/organizations/<int:organization_id>/categories/<int:category_id>/scores/recent/<int:minutes>', methods=['GET'])
@jwt_required()
def get_recent_scores_by_organization_and_category(organization_id, category_id, minutes):
    """
    Fetch all scores for a particular organization and category from the last `x` minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Calculate the time threshold
        time_threshold = datetime.now() - timedelta(minutes=minutes)

        # Query the database for scores
        scores = (
            session.query(CategoryScore)
            .join(CustomerInteraction, CustomerInteraction.customer_interaction_id == CategoryScore.customer_interaction_id)
            .filter(
                CustomerInteraction.organization_id == organization_id,
                CategoryScore.category_id == category_id,
                CustomerInteraction.created_at >= time_threshold
            )
            .all()
        )

        # Serialize scores to JSON
        scores_data = [
            {
                "category_score_id": score.category_score_id,
                "customer_interaction_id": score.customer_interaction_id,
                "score": score.score,
                "created_at": interaction.created_at.isoformat()  # Convert datetime to string
            }
            for score in scores
            for interaction in session.query(CustomerInteraction)
            .filter(CustomerInteraction.customer_interaction_id == score.customer_interaction_id)
            .all()
        ]

        return jsonify(scores_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching recent scores: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


from datetime import datetime, timedelta

@application.route('/organizations/<int:organization_id>/questions/<int:question_id>/answers/recent/<int:minutes>', methods=['GET'])
@jwt_required()
def get_recent_answers_by_organization_and_question(organization_id, question_id, minutes):
    """
    Fetch all answers for a particular organization and question from the last `x` minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Calculate the time threshold
        time_threshold = datetime.now() - timedelta(minutes=minutes)

        # Query the database for answers
        answers = (
            session.query(Answer)
            .join(CustomerInteraction, CustomerInteraction.customer_interaction_id == Answer.customer_interaction_id)
            .filter(
                CustomerInteraction.organization_id == organization_id,
                Answer.question_id == question_id,
                CustomerInteraction.created_at >= time_threshold
            )
            .all()
        )

        # Serialize answers to JSON
        answers_data = [
            {
                "answer_id": answer.answer_id,
                "customer_interaction_id": answer.customer_interaction_id,
                "question_id": answer.question_id,
                "answer": answer.answer,
                "created_at": interaction.created_at.isoformat()  # Convert datetime to string
            }
            for answer in answers
            for interaction in session.query(CustomerInteraction)
            .filter(CustomerInteraction.customer_interaction_id == answer.customer_interaction_id)
            .all()
        ]

        return jsonify(answers_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching recent answers: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/configurations', methods=['GET'])
@jwt_required()
def get_configurations_by_organization(organization_id):
    """
    Fetch all configurations for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for configurations belonging to the organization
        configurations = session.query(Configuration).filter_by(organization_id=organization_id).all()

        # Serialize configurations to JSON
        configurations_data = [
            {
                "configuration_id": config.configuration_id,
                "name": config.name,
                "description": config.description,
                "questions": [
                    {"question_id": cq.question_id, "question_text": cq.question.question}
                    for cq in config.questions
                ],
                "categories": [
                    {"category_id": cc.category_id, "category_name": cc.category.name}
                    for cc in config.categories
                ],
                "standard": {
                    "standard_id": config.standard.standard_id,
                    "standard_name": config.standard.standard.name
                } if config.standard else None
            }
            for config in configurations
        ]

        return jsonify(configurations_data), 200

    except Exception as e:
        application.logger.error(f"Error fetching configurations: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/configurations', methods=['POST'])
@jwt_required()
def create_configuration(organization_id):
    """
    Create a new configuration for a specific organization.

    Request:
    {
        "name": "Customer Feedback Configuration",
        "description": "A configuration for analyzing customer feedback.",
        "questions": [1, 2],  // List of question IDs
        "categories": [3, 4], // List of category IDs
        "standard": 5         // Standard ID (optional)
    }
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Parse the request data
        data = request.get_json()
        name = data.get("name")
        description = data.get("description", "")
        question_ids = data.get("questions", [])
        category_ids = data.get("categories", [])
        standard_id = data.get("standard")

        if not name:
            return jsonify({"error": "Configuration name is required"}), 400

        # Create the configuration
        new_configuration = Configuration(
            organization_id=organization_id,
            name=name,
            description=description
        )
        session.add(new_configuration)
        session.commit()

        # Add related questions
        for question_id in question_ids:
            session.add(ConfigurationQuestion(configuration_id=new_configuration.configuration_id, question_id=question_id))

        # Add related categories
        for category_id in category_ids:
            session.add(ConfigurationCategory(configuration_id=new_configuration.configuration_id, category_id=category_id))

        # Add related standard
        if standard_id:
            session.add(ConfigurationStandard(configuration_id=new_configuration.configuration_id, standard_id=standard_id))

        session.commit()

        return jsonify({
            "message": "Configuration created successfully",
            "configuration_id": new_configuration.configuration_id
        }), 201

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error creating configuration: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/configurations/<int:configuration_id>', methods=['DELETE'])
@jwt_required()
def delete_configuration(organization_id, configuration_id):
    """
    Delete a configuration for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the configuration to ensure it exists and belongs to the organization
        configuration = session.query(Configuration).filter_by(
            configuration_id=configuration_id,
            organization_id=organization_id
        ).first()

        if not configuration:
            return jsonify({"error": "Configuration not found"}), 404

        # Delete the configuration
        session.delete(configuration)
        session.commit()

        return jsonify({
            "message": "Configuration deleted successfully",
            "configuration_id": configuration_id
        }), 200

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error deleting configuration: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/agents/<int:agent_id>', methods=['DELETE'])
@jwt_required()
def delete_agent(organization_id, agent_id):
    """
    Delete an agent for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the agent to ensure it exists and belongs to the organization
        agent = session.query(Agent).filter_by(
            agent_id=agent_id,
            organization_id=organization_id
        ).first()

        if not agent:
            return jsonify({"error": "Agent not found"}), 404

        # Delete the agent
        session.delete(agent)
        session.commit()

        return jsonify({
            "message": "Agent deleted successfully",
            "agent_id": agent_id
        }), 200

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error deleting agent: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/questions/<int:question_id>', methods=['DELETE'])
@jwt_required()
def delete_question(organization_id, question_id):
    """
    Delete a question for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the question to ensure it exists and belongs to the organization
        question = session.query(Question).filter_by(
            question_id=question_id,
            organization_id=organization_id
        ).first()

        if not question:
            return jsonify({"error": "Question not found"}), 404

        # Delete the question
        session.delete(question)
        session.commit()

        return jsonify({
            "message": "Question deleted successfully",
            "question_id": question_id
        }), 200

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error deleting question: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/categories/<int:category_id>', methods=['DELETE'])
@jwt_required()
def delete_category(organization_id, category_id):
    """
    Delete a category for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the category to ensure it exists and belongs to the organization
        category = session.query(Category).filter_by(
            category_id=category_id,
            organization_id=organization_id
        ).first()

        if not category:
            return jsonify({"error": "Category not found"}), 404

        # Delete the category
        session.delete(category)
        session.commit()

        return jsonify({
            "message": "Category deleted successfully",
            "category_id": category_id
        }), 200

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error deleting category: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/standards/<int:standard_id>', methods=['DELETE'])
@jwt_required()
def delete_standard(organization_id, standard_id):
    """
    Delete a standard for a specific organization.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Query the standard to ensure it exists and belongs to the organization
        standard = session.query(Standard).filter_by(
            standard_id=standard_id,
            organization_id=organization_id
        ).first()

        if not standard:
            return jsonify({"error": "Standard not found"}), 404

        # Delete the standard
        session.delete(standard)
        session.commit()

        return jsonify({
            "message": "Standard deleted successfully",
            "standard_id": standard_id
        }), 200

    except Exception as e:
        session.rollback()
        application.logger.error(f"Error deleting standard: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/segments', methods=['GET'])
@jwt_required()
def get_audio_segments_with_speaker_labels_sentiments_and_silent_periods(organization_id, interaction_id):
    """
    Retrieve audio segments from S3, map the speaker labels using the speaker_labels table,
    include sentiments for each segment, and silent periods from the silent_periods table.
    Ensure the interaction is in the 'COMPLETED' state.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Fetch the interaction
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one()

        # Check if the interaction is in the 'COMPLETED' state
        if interaction.status != "COMPLETED":
            return jsonify({"error": "Interaction is not in 'COMPLETED' state"}), 400

        # Fetch the speaker mappings
        speaker_mappings = session.query(SpeakerMapping).filter_by(
            customer_interaction_id=interaction_id
        ).all()
        speaker_map = {mapping.speaker_label: mapping.role for mapping in speaker_mappings}

        # Retrieve the transcript from S3
        try:
            s3_object = s3_client.get_object(
                Bucket=S3_TRANSCRIPT_BUCKET_NAME, Key=interaction.transcript_filename
            )
            transcript_data = json.loads(s3_object['Body'].read().decode('utf-8'))
            transcript_data = transcript_data.get('results', {})
        except ClientError as e:
            application.logger.error(f"Error fetching transcript from S3: {e}")
            return jsonify({"error": "Unable to fetch transcript from S3"}), 500

        # Fetch sentiments for the interaction
        sentiments = session.query(Sentiment).filter_by(
            customer_interaction_id=interaction_id
        ).all()
        sentiment_map = {sentiment.segment_id: sentiment.sentiment for sentiment in sentiments}

        # Update speaker labels and add sentiments in the transcript data
        updated_segments = []
        for segment in transcript_data.get('audio_segments', []):
            updated_segments.append({
                "segment_id": segment['id'],
                "start_time": segment['start_time'],
                "end_time": segment['end_time'],
                "transcript": segment['transcript'],
                "speaker_label": speaker_map.get(segment['speaker_label'], segment['speaker_label']),
                "sentiment": sentiment_map.get(segment['id'], "Unknown")  # Default to "Unknown" if sentiment not found
            })

        # Fetch silent periods from the silent_periods table
        silent_periods = session.query(SilentPeriod).filter_by(
            customer_interaction_id=interaction_id
        ).all()
        silent_periods_data = [
            {
                "silent_period_id": period.silent_period_id,
                "start_time_sec": period.start_time_sec,
                "end_time_sec": period.end_time_sec
            }
            for period in silent_periods
        ]

        return jsonify({
            "interaction_id": interaction_id,
            "segments": updated_segments,
            "silent_periods": silent_periods_data
        }), 200

    except NoResultFound:
        return jsonify({"error": "Interaction not found"}), 404

    except Exception as e:
        application.logger.error(f"Error processing request: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()



@application.route('/organizations/<int:organization_id>/interactions/<int:interaction_id>/download', methods=['GET'])
@jwt_required()
def download_audio_file(organization_id, interaction_id):
    """
    Download the audio file for a specific interaction.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Fetch the interaction
        interaction = session.query(CustomerInteraction).filter_by(
            customer_interaction_id=interaction_id,
            organization_id=organization_id
        ).one_or_none()

        if not interaction:
            return jsonify({"error": "Interaction not found"}), 404

        # Retrieve the audio file from S3
        try:
            audio_object = s3_client.get_object(
                Bucket=S3_AUDIO_BUCKET_NAME,
                Key=interaction.audio_filename
            )
            content_type = audio_object['ContentType']  # Retrieve the MIME type from S3
        except ClientError as e:
            application.logger.error(f"Error fetching audio file from S3: {e}")
            return jsonify({"error": "Unable to fetch audio file"}), 500

        # Send the audio file to the client
        return send_file(
            io.BytesIO(audio_object['Body'].read()),
            mimetype=content_type,  # Use MIME type from S3
            as_attachment=True,
            download_name=interaction.audio_filename
        )

    except Exception as e:
        application.logger.error(f"Error handling audio download: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


##### Agents page #####

@application.route('/organizations/<int:organization_id>/agents/<int:agent_id>/interactions', methods=['GET'])
@jwt_required()
def get_agent_interactions_in_last_x_minutes(organization_id, agent_id):
    """
    Fetch customer interactions for a specific agent from the last X minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Get the 'minutes' parameter from the query string
        minutes = request.args.get('minutes', type=int)
        if not minutes or minutes <= 0:
            return jsonify({"error": "'minutes' must be a positive integer"}), 400

        # Calculate the time range
        cutoff_time = datetime.now() - timedelta(minutes=minutes)

        # Query the database for interactions associated with the agent in the given time range
        interactions = session.query(CustomerInteraction).filter(
            CustomerInteraction.organization_id == organization_id,
            CustomerInteraction.agent_id == agent_id,
            CustomerInteraction.created_at >= cutoff_time
        ).all()

        # Serialize the interactions
        interactions_data = [
            {
                "customer_interaction_id": interaction.customer_interaction_id,
                "organization_id": interaction.organization_id,
                "agent_id": interaction.agent_id,
                "audio_filename": interaction.audio_filename,
                "transcript_filename": interaction.transcript_filename,
                "analysis_filename": interaction.analysis_filename,
                "name": interaction.name,
                "status": interaction.status,
                "created_at": interaction.created_at.isoformat()  # Convert datetime to string
            }
            for interaction in interactions
        ]

        return jsonify({
            "agent_id": agent_id,
            "organization_id": organization_id,
            "time_range": f"Last {minutes} minutes",
            "interactions": interactions_data
        }), 200

    except Exception as e:
        application.logger.error(f"Error fetching interactions for agent: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/agents/<int:agent_id>/category-scores', methods=['GET'])
@jwt_required()
def get_agent_category_scores_in_last_x_minutes(organization_id, agent_id):
    """
    Fetch category scores for a specific agent from the last X minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Get the 'minutes' parameter from the query string
        minutes = request.args.get('minutes', type=int)
        if not minutes or minutes <= 0:
            return jsonify({"error": "'minutes' must be a positive integer"}), 400

        # Calculate the time range
        cutoff_time = datetime.now() - timedelta(minutes=minutes)

        # Query the database for category scores associated with the agent in the given time range
        category_scores = session.query(Category, CategoryScore, CustomerInteraction).join(
            CategoryScore, Category.category_id == CategoryScore.category_id
        ).join(
            CustomerInteraction, CustomerInteraction.customer_interaction_id == CategoryScore.customer_interaction_id
        ).filter(
            CustomerInteraction.organization_id == organization_id,
            CustomerInteraction.agent_id == agent_id,
            CustomerInteraction.created_at >= cutoff_time
        ).all()

        # Serialize the results
        category_scores_data = [
            {
                "category_id": category.category_id,
                "category_name": category.name,
                "score": category_score.score,
                "interaction_id": interaction.customer_interaction_id,
                "interaction_created_at": interaction.created_at.isoformat()
            }
            for category, category_score, interaction in category_scores
        ]

        return jsonify({
            "agent_id": agent_id,
            "organization_id": organization_id,
            "time_range": f"Last {minutes} minutes",
            "category_scores": category_scores_data
        }), 200

    except Exception as e:
        application.logger.error(f"Error fetching category scores for agent: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/organizations/<int:organization_id>/agents/<int:agent_id>/questions-answers', methods=['GET'])
@jwt_required()
def get_agent_questions_and_answers_in_last_x_minutes(organization_id, agent_id):
    """
    Fetch questions and their answers for a specific agent from the last X minutes.
    """
    session = None
    try:
        session = Session()
        user_id = get_current_user()['user_id']

        # Check if the user is authorized to access this organization
        if not authenticate_org_member(organization_id, user_id):
            return jsonify({"error": "Unauthorized"}), 401

        # Get the 'minutes' parameter from the query string
        minutes = request.args.get('minutes', type=int)
        if not minutes or minutes <= 0:
            return jsonify({"error": "'minutes' must be a positive integer"}), 400

        # Calculate the time range
        cutoff_time = datetime.now() - timedelta(minutes=minutes)

        # Query the database for questions and answers associated with the agent in the given time range
        questions_answers = session.query(Question, Answer, CustomerInteraction).join(
            Answer, Question.question_id == Answer.question_id
        ).join(
            CustomerInteraction, CustomerInteraction.customer_interaction_id == Answer.customer_interaction_id
        ).filter(
            CustomerInteraction.organization_id == organization_id,
            CustomerInteraction.agent_id == agent_id,
            CustomerInteraction.created_at >= cutoff_time
        ).all()

        # Serialize the results
        questions_answers_data = [
            {
                "question_id": question.question_id,
                "question_text": question.question,
                "answer_id": answer.answer_id,
                "answer_text": answer.answer,
                "interaction_id": interaction.customer_interaction_id,
                "interaction_created_at": interaction.created_at.isoformat()
            }
            for question, answer, interaction in questions_answers
        ]

        return jsonify({
            "agent_id": agent_id,
            "organization_id": organization_id,
            "time_range": f"Last {minutes} minutes",
            "questions_answers": questions_answers_data
        }), 200

    except Exception as e:
        application.logger.error(f"Error fetching questions and answers for agent: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()


@application.route('/users/<int:user_id>/organizations', methods=['GET'])
@jwt_required()
def get_organizations_for_user(user_id):
    """
    Fetch all organizations associated with a specific user.
    """
    session = None
    try:
        session = Session()

        # Get the current user's ID from the JWT
        current_user_id = get_current_user()['user_id']

        # Ensure the user is fetching their own data
        if current_user_id != user_id:
            return jsonify({"error": "Unauthorized"}), 401

        # Query the database for organizations associated with the user
        organizations = session.query(Organization).join(
            OrganizationMember, Organization.organization_id == OrganizationMember.organization_id
        ).filter(OrganizationMember.user_id == user_id).all()

        # Serialize the results
        organizations_data = [
            {
                "organization_id": org.organization_id,
                "name": org.name
            }
            for org in organizations
        ]

        return jsonify({
            "user_id": user_id,
            "organizations": organizations_data
        }), 200

    except Exception as e:
        application.logger.error(f"Error fetching organizations for user: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

    finally:
        if session:
            session.close()
