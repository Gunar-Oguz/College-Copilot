import streamlit as st
import requests
st.title("ColegeCopilot")
question = st.text_input("Ask a question about colleges: ")

if st.button("Get Schools"):
    response = requests.get("http://localhost:8000/schools")
    schools = response.json()
    st.write(schools)