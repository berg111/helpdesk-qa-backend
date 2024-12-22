import psycopg2
import openai
import json
import time
import os
import boto3
from botocore.exceptions import ClientError

AWS_REGION = "us-west-2"
openai.api_key = os.getenv('OPENAI_KEY')
# s3 client
S3_AUDIO_BUCKET_NAME = "customer-service-qa-audio"
S3_TRANSCRIPT_BUCKET_NAME = "customer-service-qa-transcripts"
s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION
)
# RDS client
def get_secret():
    secret_name = "prod/customer-service-qa/main-db"
    region_name = AWS_REGION
    # Create a Secrets Manager client
    session = boto3.session.Session()
    secrets_manager_client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )
    try:
        get_secret_value_response = secrets_manager_client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        # For a list of exceptions thrown, see
        # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
        raise e
    secret = json.loads(get_secret_value_response['SecretString'])
    return secret['username'], secret['password']

AWS_DATABASE_USER, AWS_DATABASE_PASS = get_secret()
AWS_DATABASE_ENDPOINT = os.getenv("AWS_DATABASE_ENDPOINT")
AWS_DATABASE_PORT = os.getenv("AWS_DATABASE_PORT")
AWS_DATABASE_NAME = os.getenv("AWS_DATABASE_NAME")
DATABASE_URI = f"postgresql://{AWS_DATABASE_USER}:{AWS_DATABASE_PASS}@{AWS_DATABASE_ENDPOINT}:{AWS_DATABASE_PORT}/{AWS_DATABASE_NAME}"

def lambda_handler(event, context):
    """
    AWS Lambda handler for processing transcript analysis jobs.
    The event is triggered by SQS messages containing job details.
    """
    
    # Test connection
    # conn = psycopg2.connect(
    #     host=AWS_DATABASE_ENDPOINT,
    #     database=AWS_DATABASE_NAME,
    #     user=AWS_DATABASE_USER,
    #     password=AWS_DATABASE_PASS
    # )
    # cursor = conn.cursor()
    # select_all_customer_interactions = """
    #     SELECT * FROM customer_interactions
    # """
    # cursor.execute(select_all_customer_interactions)
    # print(cursor.fetchall())
    # cursor.close()
    # conn.close()
    messages = [
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "What is 1 + 1?"}
    ]
    # Make request to llm using prompt
    MODEL = "gpt-4o-2024-08-06"
    response = openai.chat.completions.create(
        model=MODEL,
        messages=messages
    )
    results_string = response.choices[0].message.content
    print(results_string)
    return

    for record in event['Records']:
        # Parse the SQS message
        try:
            body = json.loads(record['body'])
            category_ids = body['category_ids']
            standard_id = body['standard_id']
            question_ids = body['question_ids']
            customer_interaction_id = body['customer_interaction_id']

            # Block and wait for transcript job to finish
            customer_interaction = get_customer_interaction_by_id(customer_interaction_id)
            transcript_filename = customer_interaction["transcript_filename"]
            while True:
                # Check if transcript is done processing
                if check_transcript_done_processing(transcript_filename):
                    break
                time.sleep(2.5)

            # Get transcript, categories, standards, and questions from RDS using IDs
            categories = {}
            for id in category_ids:
                category_name = get_category_by_id(id)
                categories[id] = category_name
            questions = {}
            for id in question_ids:
                question_text = get_question_by_id(id)
                questions[id] = question_text
            transcript = get_transcript_by_filename(transcript_filename)
            standard = get_standard_by_id(standard_id)
            
            # Analyze transcript
            analysis_results = analyze_transcript(transcript, categories, standard, questions)
            
            # Save results to RDS
            save_analysis(customer_interaction_id, analysis_results)

        except Exception as e:
            print(f"Error processing record {record}: {e}")
            continue

    return {
        "statusCode": 200,
        "body": json.dumps("Batch processed successfully.")
    }

def check_transcript_done_processing(filename):
    try:
        response = s3_client.get_object(Bucket=S3_TRANSCRIPT_BUCKET_NAME, Key=filename)
        return True
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == "NoSuchKey":
            print(f"{filename} not found")
            return False
        else:
            raise e
    except Exception as e:
        raise e

def get_customer_interaction_by_id(id):
    pass # TODO

def get_category_by_id(id):
    pass # TODO

def get_question_by_id(id):
    pass # TODO

def get_transcript_by_filename(filename):
    # get transcript from s3
    # return only the audio_segments portion
    pass # TODO

def get_standard_by_id(id):
    pass # TODO

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
        categories = {232: "Politeness", 182: "Clarity", 433: "Resolution"}
        standard = "The agent should greet the customer, listen carefully, and resolve their issue politely."
        questions = {117: "Did the agent greet the customer?", 54: "Did the agent resolve the customer's issue?"}
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
                "question_id": "117"
                },
                ...
            ],
            "comparison": "The agent did not ...",
            "scores": [
                {
                "category_id": "232",
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
                                "category_id": {"type": "string"},
                                "score": {"type": "number"}
                            },
                            "required": ["category_id", "score"],
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
                                "question_id": {"type": "string"},
                                "answer": {"type": "string"}
                            },
                            "required": ["question_id", "answer"],
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
    3. Score the conversation based on these categories:
    {" ".join([f'- category_id {cat_key}: {categories[cat_key]}' for cat_key in categories.keys()])}.
    4. Compare the conversation against this standard: "{standard}".
    5. Summarize the entire interaction in a single paragraph.
    6. Answer these questions:
    {" ".join([f'- question_id {q_key}: {questions[q_key]}' for q_key in questions.keys()])}.

    Return the output in structured JSON format according to the provided schema.
    """}
    ]
    # Make request to llm using prompt
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
    return results_dict

def save_analysis(customer_interaction_id, analysis_results):
    """
    Saves analysis results to RDS.
    """
    try:
        # TODO
        pass
        
    except ClientError as e:
        print(f"Error saving analysis results: {e}")
        raise