from fastapi import FastAPI
app = FastAPI()

schools_list = [
    {"name": "Stanford", "state": "CA"},
    {"name": "MIT", "state": "MA"},
    {"name": "Harvard", "state": "MA"}
]

states_list = [
    {"name": "Massachusettes", "abr": 'MA'},
    {"name": "California", "abr": "CA"}
]

@app.get("/")
def greet():
    return {"message": "Welcome to College Copilot"}

@app.get("/schools")
def schools():
    return schools_list

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/states")
def state():
    return states_list