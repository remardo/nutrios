import os
import sys
from admin.api import app

if __name__ == "__main__":
    print("API app imported successfully")
    print(f"App title: {app.title}")