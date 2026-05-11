# cinescope-ai-movie-platform
AI-powered movie discovery platform with TMDB live catalog, Gemini chatbot, personalized recommendations, user authentication, and admin dashboard.
# CineScope: AI-Powered Movie Discovery Platform

CineScope is a modern movie discovery website built with Flask, TMDB API, Gemini AI, SQLite, HTML, CSS, and JavaScript.

It combines live movie search, personalized recommendations, optional user authentication, an admin dashboard, and a movie chatbot in a single platform.

## Features

- Live movie search using TMDB
- Trending, popular, and top-rated movie sections
- Movie detail pages with poster, rating, overview, cast, and related information
- Optional user registration and login
- Personalized recommendations based on saved preferences
- Gemini-powered movie chatbot
- Admin dashboard for user and login activity monitoring
- Local CSV fallback catalog when API access is unavailable
- Modern dark cinematic interface

## Chatbot Capabilities

The chatbot can answer movie-related questions such as:

- `Leonardo DiCaprio and Kate Winslet`
- `My Heart Will Go On`
- `Who directed Inception?`
- `Show me sci-fi movies`
- `Shah Rukh Khan`

It returns grounded movie results with clickable movie cards.

## Technologies Used

- Python
- Flask
- SQLite
- HTML
- CSS
- JavaScript
- TMDB API
- Gemini API

## Project Structure

```text
cinescope-ai-movie-platform/
│
├── app.py
├── requirements.txt
├── API_SETUP.txt
├── .env.example
├── README.md
├── .gitignore
│
├── data/
│   └── movies.csv
│
├── static/
│   └── style.css
│
└── templates/
    ├── base.html
    ├── index.html
    ├── results.html
    ├── movie_detail.html
    ├── login.html
    ├── register.html
    ├── preferences.html
    └── admin_dashboard.html
