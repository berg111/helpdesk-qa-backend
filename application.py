import os
from flask import Flask, render_template, request, url_for, redirect, jsonify
from flask_cors import CORS
import requests
from dotenv import load_dotenv
import openai


application = Flask(__name__)
cors = CORS(application)

load_dotenv()
# SOME_VAR = os.getenv('VAR_NAME')

@application.route('/')
def index():
    return "Root"

def process_transcript(standards, categories, transcript):
    '''
    standards: string | the standards to compare the transcript to.
    transcript: key-pair object / dict | the conversation we are analyzing.
    '''
    prompt = f"""
    You are a web server that compares how well a conversation between 
    an employee and a customer fits the standards and expectations 
    outlined by management. You will be supplied with the transcript of the 
    conversation, the expectations outlined by management, and a list of 
    categories to score. Your response will be in JSON format 
    and contain the following:\n
    - 'summary': A high-level written summary of the analysis for this particular 
    conversation.\n
    - 'scores': A list of tuples, where the first item is the name of the category 
    and the second item is the score for that category. The score is in the range of 
    1 to 10 inclusive and uses integers only. 
    A score of 1 indicates that the conversation did NOT meet standards, and a score 
    of 10 indicates that the conversation was exemplary and matched standards very 
    well.\n
    Now process the following request and respond as a web server:\n
    Expectations: {standards}\n
    Categories: {categories}\n
    Transcript: {transcript}\n
    """

    # make request to llm using prompt
    openai.api_key = os.getenv('OPENAI_KEY')
    # Make the request to GPT-4
    response = openai.chat.completions.create(
        model="gpt-3.5-turbo",  # Specify the model you want to use (GPT-4)
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,  # You can adjust the number of tokens here
        temperature=0.7  # Control the randomness of the output
    )

    # Print the response
    print(response.choices[0].message.content)

    return response.choices[0].message.content

@application.route('/analyze-transcript', methods=['POST'])
def analyze_transcript():
    try:
        # Get the JSON object from the request
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        # Process the JSON object
        # print("Received JSON:", data)
        results = process_transcript(data['standards'], data['categories'], data['transcript'])
        print("Results:\n", results)

        # Respond back with a success message
        return jsonify({"message": "Request processed successfully", "results": results}), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500