from fastapi import FastAPI
app = FastAPI()

schools_list = [
    {"name": "Stanford", "state": "CA"},
    {"name": "MIT", "state": "MA"},
    {"name": "Harvard", "state": "MA"}
]

@app.get("/")
def read_root():
    return{"message": "Welcome to College Copilot"}

@app.get("/schools")
def get_schools():
    return schools_list

@app.get("/health")
def health_check():
    return {"status": "healthy"}


