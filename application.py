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

def create_customer_interaction(audio_filename, transcript_filename, organization_id, agent_id):
    '''
    Create a new entry in the customer_interactions table in the DB and set processing 
    status to "PENDING".
    '''
    # TODO
    pass

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

@application.route('/upload-and-analyze', methods=['POST'])
def upload_and_analyze():
    '''
    Sends a job to worker
    '''
    # start_time = time.time()
    # Get audio files from request
    if 'audio_files' not in request.files:
        return jsonify({"error": "No audio files provided"}), 400
    files = request.files.getlist('audio_files')
    if not files or any(file.filename == '' for file in files):
        return jsonify({"error": "One or more files are missing filenames"}), 400
    
    # TODO: Get analysis settings from request
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
    # Debugging
    print(f"""
        organization_id: {type(organization_id)} {organization_id},
        agent_id: {type(agent_id)} {agent_id},
        standard_id: {type(standard_id)} {standard_id},
        category_ids: {type(category_ids)} {category_ids},
        question_ids: {type(question_ids)} {question_ids},
    """)

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
        start_transcription_job(filename)
        # Create new entry in the jobs table
        customer_interaction_id = create_customer_interaction(filename, filename, organization_id, agent_id)
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
    
    # end_time = time.time()
    # print("time:", end_time - start_time)
    return jsonify(new_customer_interactions), 200