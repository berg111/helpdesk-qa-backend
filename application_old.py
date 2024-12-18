import os
from flask import Flask, render_template, request, url_for, redirect, jsonify
from flask_cors import CORS
import requests
from dotenv import load_dotenv
import openai
import json
from textwrap import dedent
import boto3
import time
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, CustomerInteraction
from datetime import datetime

application = Flask(__name__)
cors = CORS(application)

load_dotenv()
# SOME_VAR = os.getenv('VAR_NAME')
openai.api_key = os.getenv('OPENAI_KEY')

# Configure AWS S3 and AWS transcribe
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
transcribe_client = boto3.client(
    "transcribe",
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
@application.route('/')
def index():
    transcript = get_transcript("transcription-job-1733962774-jobs16-mp3.json")
    transcript = transcript["results"]["audio_segments"]
    categories = ["Politeness", "Clarity", "Resolution"]
    standard = "The agent should greet the customer, listen carefully, and resolve their issue politely."
    questions = ["Did the agent greet the customer?", "Did the agent resolve the customer's issue?"]
    
    test_result = analyze_transcript(
        transcript=transcript,
        categories=categories,
        standard=standard,
        questions=questions
    )
    return test_result

@application.route('/get-transcript/<transcript_filename>', methods=['GET'])
def get_transcript(transcript_filename):
    '''
    Transcripts are stored in S3 (currently). And follow the 
    naming convention https://bucket-name.s3.region.amazonaws.com/object-key 
    or s3://bucket-name/object-key. If a transcript hasn't processed yet, 
    we can account for that here.
    '''
    try:
        response = s3_client.get_object(Bucket=S3_TRANSCRIPT_BUCKET_NAME, Key=transcript_filename)
        file_content = response['Body'].read().decode('utf-8')
        transcript_json = json.loads(file_content)
        return transcript_json
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == "NoSuchKey":
            print("File not found")
            return {"status": "PENDING"}
        print(f"ClientError: {error_code} - {e}")
        return {"error": f"ClientError: {error_code}"}
    except Exception as e:
        print(f"An error occurred: {e}")
        return {"error": str(e)}

def save_analysis(analysis):
    '''
    Takes the output from 'analyze_transcript' and saves it to the database.

    Example input:
        {
            "answers": [
                {
                "answer": "No",
                "question": "Did the agent greet the customer?"
                },
                ...
            ],
            "comparison": "The agent did not ...",
            "scores": [
                {
                "name": "Politeness",
                "score": 4
                },
                ...
            ],
            "sentiments": [
                {
                "segment_id": "0",
                "sentiment": "Neutral"
                },
                ...
            ],
            "silent_periods": [],
            "speaker_mapping": [
                {
                "role": "Customer",
                "speaker_label": "spk_0"
                },
                {
                "role": "Agent",
                "speaker_label": "spk_1"
                }
            ],
            "summary": "In this interaction, ..."
        }
    '''
    pass

@application.route('/analyze-transcript', methods=['POST'])
def analyze_transcript(transcript, categories, standard, questions):
    '''
    Calculates the following metrics and then calls "save_analysis":
        - Periods of silence
        - (AI) Sentiment for audio-segment (postive, negative, neutral)
        - (AI) Mapping each speaker to either 'Customer' or 'Agent'
        - (AI) Scores for a list of user-defined categories
        - (AI) An overview of how the interaction compares to a user-defined standard 
          (a textual explanation of what an interaction should look like.)
        - (AI) A text summary of the interaction
        - (AI) A list of answers to user-defined questions (ex: "Did 
          the agent greet the customer?")
    
    Example inputs:
        categories = ["Politeness", "Clarity", "Resolution"]
        standard = "The agent should greet the customer, listen carefully, and resolve their issue politely."
        questions = ["Did the agent greet the customer?", "Did the agent resolve the customer's issue?"]
        transcript = [
            {
                "end_time": "2.029",
                "id": 0,
                "items": [0, 1, 2, 3, 4, 5, 6, 7],
                "speaker_label": "spk_0",
                "start_time": "0.0",
                "transcript": "I want to move to New York,"
            },
            ...
        ]
    
    Example output:
        {
            "answers": [
                {
                "answer": "No",
                "question": "Did the agent greet the customer?"
                },
                ...
            ],
            "comparison": "The agent did not ...",
            "scores": [
                {
                "name": "Politeness",
                "score": 4
                },
                ...
            ],
            "sentiments": [
                {
                "segment_id": "0",
                "sentiment": "Neutral"
                },
                ...
            ],
            "silent_periods": [],
            "speaker_mapping": [
                {
                "role": "Customer",
                "speaker_label": "spk_0"
                },
                {
                "role": "Agent",
                "speaker_label": "spk_1"
                }
            ],
            "summary": "In this interaction, ...",
            "standard": "The agent should ..."
        }
    '''
    # Get periods of silence
    silent_periods = [] # [[start_time, end_time], ...]
    silence_threshold_seconds = 20
    for i in range(len(transcript) - 1):
        left_segment_end = float(transcript[i]['end_time'])
        right_segment_start = float(transcript[i+1]['start_time'])
        gap = right_segment_start-left_segment_end
        if gap >= silence_threshold_seconds:
            silent_periods.append([left_segment_end, right_segment_start])

    # Send transcript to AI model to get additional results
    # Define the JSON schema for output
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "transcript_analysis",
            "schema": {
                "type": "object",
                "properties": {
                    "sentiments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "segment_id": {
                                    "type": "string",
                                    "description": "The ID or number of the segment in the transcript."
                                },
                                "sentiment": {
                                    "type": "string",
                                    "enum": ["Positive", "Neutral", "Negative"],
                                    "description": "The sentiment associated with this segment."
                                }
                            },
                            "required": ["segment_id", "sentiment"],
                            "additionalProperties": False
                        },
                        "description": "List of sentiment analysis results for each segment."
                    },
                    "speaker_mapping": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "speaker_label": {
                                    "type": "string",
                                    "description": "The speaker_label specified in the transcript."
                                },
                                "role": {
                                    "type": "string",
                                    "enum": ["Customer", "Agent"],
                                    "description": "The role played by this speaker."
                                }
                            },
                            "required": ["speaker_label", "role"],
                            "additionalProperties": False
                        },
                        "description": "List of speakers and their associated roles."
                    },
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "score": {"type": "number"}
                            },
                            "required": ["name", "score"],
                            "additionalProperties": False
                        }
                    },
                    "comparison": {
                        "type": "string",
                        "description": "Comparison of the interaction against the user standard."
                    },
                    "summary": {
                        "type": "string",
                        "description": "A summary of the entire transcript."
                    },
                    "answers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "answer": {"type": "string"}
                            },
                            "required": ["question", "answer"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["sentiments", "speaker_mapping", "scores", "comparison", "summary", "answers"],
                "additionalProperties": False
            },
            "strict": True
        }
    }
    # Construct the system role and user message
    transcript_string = ""
    for segment in transcript:
        transcript_string += (f"""
        {{
        segment_id: "{segment["id"]}",
        speaker_label: "{segment["speaker_label"]}",
        segment_transcript: "{segment["transcript"]}"
        }}
        """)
    messages = [
        {"role": "system", "content": "You are an assistant that analyzes customer service transcripts and outputs structured JSON."},
        {"role": "user", "content": f"""
    Analyze the following customer service transcript:

    Transcript:
    {transcript_string}

    Perform the following tasks:
    1. Analyze sentiment for each transcript segment as Positive, Negative, or Neutral.
    2. Map each speaker_label to either 'Agent' or 'Customer'.
    3. Score the conversation based on these categories: {', '.join(categories)}.
    4. Compare the conversation against this standard: "{standard}".
    5. Summarize the entire interaction in a single paragraph.
    6. Answer these questions:
    {" ".join([f"- {q}" for q in questions])}.

    Return the output in structured JSON format according to the provided schema.
    """}
    ]
    # make request to llm using prompt
    MODEL = "gpt-4o-2024-08-06"
    response = openai.chat.completions.create(
        model=MODEL,
        messages=messages,
        response_format=response_format
    )
    results_string = response.choices[0].message.content
    results_dict = json.loads(results_string)
    results_dict['silent_periods'] = silent_periods
    results_dict['standard'] = standard
    results = jsonify(results_dict)
    return results
 
@application.route('/upload-and-transcribe', methods=['POST'])
def upload_and_transcribe():
    '''
    Uploads the audio file to s3.
    Starts the transcription job.
    Creates a record in the customer_interactions table.
    '''
    # TODO: Change audio, transcription, and analysis filenames to be better (more unique)
    session = Session()
    if 'audio_files' not in request.files:
        return jsonify({"error": "No audio files provided"}), 400

    files = request.files.getlist('audio_files')
    if not files or any(file.filename == '' for file in files):
        return jsonify({"error": "One or more files are missing filenames"}), 400

    results = []
    error_flag = False

    try:
        for file in files:
            unique_filename = f"{int(time.time())}-{file.filename}"
            audio_filename = unique_filename
            s3_file_path = f"s3://{S3_AUDIO_BUCKET_NAME}/{unique_filename}"

            # Upload file to S3
            try:
                s3_client.upload_fileobj(file, S3_AUDIO_BUCKET_NAME, unique_filename)
            except ClientError as e:
                error_flag = True
                results.append({
                    "filename": file.filename,
                    "message": "Failed to upload file to S3",
                })
                continue

            # Start transcription
            job_name = f"transcription-job-{int(time.time())}-{file.filename.replace('.', '-')}"
            try:
                print(f"Starting Job {job_name}")
                transcribe_client.start_transcription_job(
                    TranscriptionJobName=job_name,
                    Media={"MediaFileUri": s3_file_path},
                    MediaFormat='mp3',
                    LanguageCode="en-US",
                    OutputBucketName=S3_TRANSCRIPT_BUCKET_NAME,
                    Settings={
                        'ShowSpeakerLabels': True,
                        'MaxSpeakerLabels': 2
                    }
                )
            except Exception as e:
                error_flag = True
                results.append({
                    "filename": file.filename,
                    "s3_url": s3_file_path,
                    "message": f"Failed to start transcription job: {str(e)}"
                })
                continue

            # Store the record in the database
            interaction = CustomerInteraction(
                organization="ExampleOrg",  # TODO: Replace with real data
                customer="John Doe",       # TODO: Replace with real data
                agent="Agent Name",        # TODO: Replace with real data
                audio_filename=audio_filename,
                transcript_filename=f"{job_name}.json"
            )
            session.add(interaction)
            session.commit()

            results.append({
                "filename": file.filename,
                "audio_filename": audio_filename,
                "transcript_filename": f"{job_name}.json",
                "message": "File uploaded and transcription job started"
            })

        return_msg = "All files processed successfully" if not error_flag else "Some files failed to process"
        return jsonify({"message": return_msg, "results": results}), 200

    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        session.close()
