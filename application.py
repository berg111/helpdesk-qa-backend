import os
from flask import Flask, render_template, request, url_for, redirect, jsonify
from flask_cors import CORS
import requests
from dotenv import load_dotenv
import json
from textwrap import dedent
import boto3
import time
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, CustomerInteraction
from datetime import datetime
import uuid
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor

application = Flask(__name__)
cors = CORS(application)

load_dotenv()
# SOME_VAR = os.getenv('VAR_NAME')

# Configure AWS S3
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = "us-west-2"
S3_AUDIO_BUCKET_NAME = "customer-service-qa-audio"
S3_TRANSCRIPT_BUCKET_NAME = "customer-service-qa-transcripts"
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Configure AWS RDS connection
AWS_DATABASE_USER = os.getenv("AWS_DATABASE_USER")
AWS_DATABASE_PASS = os.getenv("AWS_DATABASE_PASS")
AWS_DATABASE_ENDPOINT = os.getenv("AWS_DATABASE_ENDPOINT")
AWS_DATABASE_PORT = os.getenv("AWS_DATABASE_PORT")
AWS_DATABASE_NAME = os.getenv("AWS_DATABASE_NAME")
DATABASE_URI = f"postgresql://{AWS_DATABASE_USER}:{AWS_DATABASE_PASS}@{AWS_DATABASE_ENDPOINT}:{AWS_DATABASE_PORT}/{AWS_DATABASE_NAME}"
engine = create_engine(DATABASE_URI)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine) # Create tables if they don't exist

AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = "us-west-2"
sqs = boto3.client(
    "sqs",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL") # TODO: get url

@application.route('/')
def index():
    return "index"

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
    s3_file_path = f"s3://{S3_AUDIO_BUCKET_NAME}/{unique_filename}"
    # Upload file to S3
    try:
        print("Starting upload for:", filename)
        s3_client.upload_fileobj(file, S3_AUDIO_BUCKET_NAME, unique_filename)
        print("Finished upload for:", filename)
    except ClientError as e:
        raise e
    return unique_filename

def create_customer_interaction(audio_filename, organization_id, agent_id):
    '''
    Create a new entry in the customer_interactions table in the DB and set processing 
    status to "PENDING".
    '''
    # TODO
    pass

@application.route('/upload-and-analyze', methods=['POST'])
def upload_and_analyze():
    '''
    Sends a job to worker
    '''
    start_time = time.time()
    # Get audio files from request
    if 'audio_files' not in request.files:
        return jsonify({"error": "No audio files provided"}), 400
    files = request.files.getlist('audio_files')
    if not files or any(file.filename == '' for file in files):
        return jsonify({"error": "One or more files are missing filenames"}), 400
    
    # TODO: Get analysis settings from request
    organization_id = 0 # the organization uploading the audio
    agent_id = 0 # the agent heard in the audio
    category_ids = [0] # the categories to score on
    standard_id = 0 # the standard to compare to
    question_ids = [0] # the questions to answer

    # Upload files to s3
    filenames = []
    # Create a thread pool for concurrent uploads
    with ThreadPoolExecutor(max_workers=10) as executor:
        for file in files:
            filename = generate_unique_filename(file.filename)
            executor.submit(upload_audio_to_s3, file, filename)
            filenames.append(filename)

    new_customer_interactions = []
    for filename in filenames:
        # Create new entry in the jobs table
        customer_interaction_id = create_customer_interaction(filename, organization_id, agent_id)
        new_customer_interactions.append(customer_interaction_id)
        # Send a job to SQS
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps({
                "customer_interaction_id": customer_interaction_id,
                "category_ids": category_ids,
                "standard_id": standard_id,
                "question_ids": question_ids
            })
        )
    
    end_time = time.time()
    print("time:", end_time - start_time)
    return jsonify(new_customer_interactions), 200