import os
from flask import Flask, render_template, request, url_for, redirect, jsonify
from flask_cors import CORS
import requests
from dotenv import load_dotenv


application = Flask(__name__)
cors = CORS(application)

load_dotenv()
# SOME_VAR = os.getenv('VAR_NAME')

@application.route('/')
def index():
    return "Root"
