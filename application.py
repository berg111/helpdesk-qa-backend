import os
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from dotenv import load_dotenv
import json
import boto3
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, CustomerInteraction, User
import uuid
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity, verify_jwt_in_request, set_access_cookies
)

application = Flask(__name__)
bcrypt = Bcrypt(application)
application.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
application.config["JWT_TOKEN_LOCATION"] = ["cookies"]   # Use cookies for token storage
application.config["JWT_ACCESS_COOKIE_NAME"] = "access_token"
application.config["JWT_COOKIE_CSRF_PROTECT"] = False
jwt = JWTManager(application)
CORS(application, 
     supports_credentials=True, 
     origins=["http://localhost:3000"], 
     resources={r"/*": {"origins": "http://localhost:3000"}}
)
# cors = CORS(application) # TODO: Allow requests only from frontend. Something like CORS(app, resources={r"/*": {"origins": "https://your-frontend-domain.com"}})

load_dotenv()
# SOME_VAR = os.getenv('VAR_NAME')

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

@application.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = 'http://localhost:3000'  # Frontend origin
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
            access_token = create_access_token(identity=str({"user_id": user.user_id, "email": user.email}))
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

@application.route('/whoami', methods=['GET'])
@jwt_required()
def whoami():
    try:
        current_user = get_jwt_identity()
        print(f"JWT Identity: {current_user}")  # Debugging
        return jsonify({"logged_in_as": current_user}), 200
    except Exception as e:
        print(f"Error: {e}")  # Log errors for debugging
        return jsonify({"error": "Unauthorized"}), 401

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


@application.route('/upload-and-analyze', methods=['POST'])
def upload_and_analyze():
    '''
    Sends a job to worker
    '''
    # Get audio files from request
    if 'audio_files' not in request.files:
        return jsonify({"error": "No audio files provided"}), 400
    files = request.files.getlist('audio_files')
    if not files or any(file.filename == '' for file in files):
        return jsonify({"error": "One or more files are missing filenames"}), 400
    
    organization_id = int(request.form.get('organization_id')) # the organization uploading the audio
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