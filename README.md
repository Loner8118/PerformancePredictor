# Performance Predictor

**Performance Predictor** is a web application performance analysis and scalability prediction system designed to evaluate a web application's behavior under increasing load.

The system takes a GitHub repository, automatically prepares and runs the application in an isolated Docker environment, performs automated load testing, collects runtime performance metrics, applies mathematical performance models, and produces capacity, scalability, bottleneck, and engineering recommendations.

The project focuses on **mathematical performance analysis rather than machine-learning-based prediction**.

---

## Features

### Automated Application Analysis

* GitHub repository cloning
* Repository validation
* Automatic framework detection
* Application entry-point detection
* Docker-based isolated execution
* Application health checking
* Route discovery

### Automated Load Testing

* Automatic Locust test generation
* Multiple concurrent-user levels
* Automated load-test execution
* Throughput measurement
* Response-time measurement
* Error-rate measurement
* Runtime resource monitoring

### Mathematical Performance Analysis

The system analyzes collected performance data using multiple mathematical models:

* Universal Scalability Law (USL)
* Little's Law
* Queueing analysis
* Utilization-based bottleneck analysis
* Asymptotic scalability bounds
* Capacity estimation
* Scalability prediction

### Recommendation Engine

The recommendation engine converts the mathematical results and observed runtime behavior into evidence-based engineering recommendations.

Recommendations can consider:

* Capacity limits
* High utilization
* Queueing behavior
* Scalability degradation
* Bottleneck behavior
* Response-time degradation
* Error behavior
* Confidence and reliability of the prediction

---

# System Workflow

The overall Version 2 pipeline is:

```text
GitHub Repository
       │
       ▼
Repository Validation
       │
       ▼
Repository Clone
       │
       ▼
Framework Detection
       │
       ▼
Entry-Point Detection
       │
       ▼
Docker Build & Run
       │
       ▼
Health Check
       │
       ▼
Route Discovery
       │
       ▼
Locust Test Generation
       │
       ▼
Automated Load Testing
       │
       ▼
Runtime Monitoring
       │
       ▼
Performance Metrics
       │
       ▼
Mathematical Analysis
       │
       ├── USL
       ├── Little's Law
       ├── Queueing
       ├── Bottleneck
       ├── Capacity
       └── Scalability
       │
       ▼
Recommendations
       │
       ▼
Performance Report / Dashboard
```

---

# Project Structure

```text
PerformancePredictor/
│
├── automation/
│   ├── docker_manager.py
│   ├── entry_point_locator.py
│   ├── framework_detector.py
│   ├── github_manager.py
│   ├── health_checker.py
│   ├── locust_generator.py
│   ├── locust_runner.py
│   ├── metrics_collector.py
│   ├── route_discovery.py
│   └── validator.py
│
├── metrics/
│   ├── csv_parser.py
│   └── runtime_monitor.py
│
├── prediction/
│   ├── usl.py
│   ├── little_law.py
│   ├── queueing.py
│   ├── bottleneck.py
│   ├── capacity.py
│   ├── scalability.py
│   └── recommendation.py
│
├── frontend/
│   ├── css/
│       └─ style.css
│   └── js/
│       └─ app.js
│   └── index.html
│
├── requirements.txt
├── .gitignore
├── README.md
└── app.py
```

> The exact file list may vary slightly depending on the current repository structure.

---

# Requirements

The project requires:

* Python 3.11
* Git
* Docker
* Locust
* A modern web browser
* Internet connection for cloning GitHub repositories

Recommended versions used during development:

```text
Python 3.11.0
Git 2.49.0
Docker 29.6.2
Locust 2.29.0
```

---

# Running the Project Locally

## 1. Clone the repository

Clone the Performance Predictor repository:

```bash
git clone https://github.com/Loner8118/PerformancePredictor
```

Move into the project directory:

```bash
cd PerformancePredictor
```

---

# 2. Create a Python Virtual Environment

Creating a virtual environment is recommended so that project dependencies remain isolated from the system Python installation.

### Windows

```powershell
python -m venv venv
```

Activate it:

```powershell
.\venv\Scripts\activate
```

After activation, the terminal should show something similar to:

```text
(venv) C:\...\PerformancePredictor>
```

### Linux / macOS

```bash
python3 -m venv venv
```

Activate it:

```bash
source venv/bin/activate
```

---

# 3. Upgrade pip

After activating the virtual environment:

```bash
python -m pip install --upgrade pip
```

---

# 4. Install Python Dependencies

Install the project's dependencies using:

```bash
pip install -r requirements.txt
```

The project uses Python libraries for functionality including:

* Flask
* GitPython
* Docker SDK
* Locust
* Requests
* Pandas
* psutil
* python-dotenv

The exact installed versions should be taken from `requirements.txt`.

---

# 5. Verify the Environment

Check Python:

```bash
python --version
```

Check Git:

```bash
git --version
```

Check Docker:

```bash
docker --version
```

Check Locust:

```bash
locust --version
```

Docker must be running before using the automated application execution and load-testing pipeline.

---

# 6. Start the Application

Activate the virtual environment first:

### Windows

```powershell
.\venv\Scripts\activate
```

Then start the application using the project's configured entry point.

For example, if the backend entry point is `app.py`:

```bash
python app.py
```

The terminal will display the local server address.

Open that address in a browser, typically:

```text
http://127.0.0.1:5000
```

or:

```text
http://localhost:5000
```

---

# Using Performance Predictor

## 1. Provide a GitHub Repository

The application accepts a GitHub repository containing the web application to be analyzed.

The system validates the repository before attempting to execute it.

---

## 2. Repository Analysis

After the repository is provided, Performance Predictor performs automated analysis including:

1. Repository cloning
2. Framework detection
3. Entry-point detection
4. Application validation

The system currently provides strong detection support for Flask applications and uses source-code analysis to identify relevant application structures.

---

## 3. Docker Execution

The target application is built and executed inside Docker.

This provides an isolated environment for running the application being tested.

The system then performs a health check to verify that the application is responding before continuing with performance testing.

---

## 4. Route Discovery

Available application routes are discovered so that they can be used during automated testing.

For example:

```text
/
 /about
 /contact
 /dashboard
 /login
 /register
```

The discovered routes are then used to generate the load-testing configuration.

---

# Load Testing

Performance Predictor uses **Locust** for automated load testing.

The system can execute tests using multiple concurrent-user levels.

A typical test configuration can include:

```text
20 users
50 users
100 users
200 users
300 users
500 users
```

For each load level, performance measurements are collected.

The collected information includes metrics such as:

* Throughput
* Response time
* Number of requests
* Number of failures
* Error rate
* Runtime CPU utilization
* Memory utilization
* Network activity

---

# Mathematical Performance Engine

The collected load-test results are passed to the mathematical analysis engine.

The purpose is not simply to report raw performance measurements, but to determine how the application behaves as concurrency increases.

---

## Universal Scalability Law

The Universal Scalability Law models the relationship between concurrency and system throughput.

The model used is:

```text
X(N) = X(1) × N / (1 + σ(N - 1) + κN(N - 1))
```

where:

* `X(N)` = throughput at concurrency `N`
* `X(1)` = throughput at one user
* `σ` = contention coefficient
* `κ` = coherency coefficient
* `N` = concurrency

The model helps identify:

* Contention
* Coherency effects
* Scalability degradation
* Peak predicted throughput
* Approximate optimal concurrency
* Saturation behavior

The implementation also considers model quality and uncertainty when determining how much confidence should be placed in extrapolated predictions.

---

## Little's Law

Little's Law relates the average number of items in a system to throughput and time spent in the system:

```text
L = λW
```

where:

* `L` = average number of items in the system
* `λ` = throughput
* `W` = average time in the system

The implementation uses measured throughput and response-time information to estimate system occupancy.

### Important distinction

Locust's configured user count is **not automatically equivalent to `L` in Little's Law**.

The model uses measured system behavior rather than simply treating the configured concurrent-user count as the average number of requests in the system.

---

# Queueing Analysis

Queueing analysis is used to examine system behavior as utilization approaches saturation.

The implementation evaluates utilization and queue-related behavior to identify situations where increasing load may cause rapidly increasing waiting time.

For example:

```text
Low utilization
      ↓
Normal operation

Higher utilization
      ↓
Increasing queueing

Near saturation
      ↓
Rapid performance degradation
```

This provides additional evidence for capacity and bottleneck decisions.

---

# Bottleneck Analysis

The bottleneck analysis examines resource utilization and demand-related behavior to determine which resource is most likely limiting system performance.

The analysis can consider:

* CPU utilization
* Memory utilization
* Network behavior
* Service demand
* Utilization
* Saturation indicators

The system also uses utilization-based reasoning to estimate whether the application is approaching a resource limit.

---

# Capacity Analysis

Capacity analysis combines measured load-test results with the mathematical analysis.

The goal is to distinguish between:

```text
Safe operating region
        ↓
Warning region
        ↓
Overloaded / breaking point
```

The resulting capacity information can include:

* Estimated safe user capacity
* Observed breaking load
* Utilization near the limit
* Capacity classification
* Supporting performance measurements

The capacity result is based on the evidence available from the performed tests rather than being treated as an unlimited theoretical prediction.

---

# Scalability Prediction

The scalability module uses the fitted scalability behavior to estimate how the application may behave at higher concurrency levels.

Example prediction targets can include:

```text
1000 users
2000 users
5000 users
```

Predictions are treated cautiously when the fitted model has poor statistical quality or when extrapolation extends significantly beyond the observed load range.

The system therefore reports prediction confidence and relevant warnings instead of presenting every extrapolated value as certain.

---

# Recommendation Engine

The recommendation engine converts the analysis results into engineering actions.

Recommendations are based on evidence obtained from:

```text
Load Test
    +
Runtime Metrics
    +
USL
    +
Little's Law
    +
Queueing
    +
Bottleneck Analysis
    +
Capacity Analysis
    +
Scalability Analysis
```

For example, when the system observes high utilization and queueing behavior near the maximum tested load, the recommendation can indicate that the current workload is approaching the application's safe operating capacity.

The recommendations are intended to explain **why** an action is suggested rather than simply producing generic optimization advice.

---

# API

The backend exposes API endpoints used by the frontend for load testing and mathematical analysis.

Important endpoints include:

```text
/api/load-testing/run
/api/prediction/usl
/api/prediction/littles-law
```

The load-testing endpoint accepts the information required to execute a performance test, including the target project and running container.

The prediction endpoints expose the calculated mathematical analysis results to the frontend.

---

# Example Analysis

A completed analysis can contain results such as:

```text
Observed Load:
500 users

Throughput:
209.96 requests/sec

Average Response Time:
49.75 ms

CPU Utilization:
51.35%

Memory Utilization:
0.36%

Error Rate:
0%

USL:
Peak throughput ≈ 183.28

USL Optimal Concurrency:
≈ 309 users

USL Saturation Estimate:
≈ 121 users

Little's Law:
L ≈ 10.45
W ≈ 0.04975 s

Queue Utilization:
≈ 0.9835

Estimated Safe Capacity:
≈ 400 users

Observed Breaking Load:
≈ 500 users
```

These values are **example results from a performance-test run**, not fixed values produced for every application.

---

# Project Architecture

```text
                    ┌─────────────────────┐
                    │   GitHub Repository  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Repository Analysis │
                    │ Validation / Clone  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Framework & Entry   │
                    │ Point Detection     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Docker Execution    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Health Check        │
                    │ Route Discovery     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Locust Load Testing │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Runtime Monitoring  │
                    │ & Metrics           │
                    └──────────┬──────────┘
                               │
                               ▼
              ┌──────────────────────────────────┐
              │       Mathematical Engine        │
              │                                  │
              │ USL │ Little's Law │ Queueing   │
              │ Bottleneck │ Capacity │ Scaling │
              └────────────────┬─────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Recommendation      │
                    │ Engine              │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Web Dashboard       │
                    │ & Results           │
                    └─────────────────────┘
```

---

# Development Environment

The project was developed and tested using:

```text
Operating System:
Windows

Python:
3.11.0

Git:
2.49.0

Docker:
29.6.2

Locust:
2.29.0
```

A Python virtual environment is used to isolate project dependencies.

---

# Deactivating the Virtual Environment

When finished working on the project:

```bash
deactivate
```

To work on the project again later:

```powershell
cd C:\path\to\PerformancePredictor
.\venv\Scripts\activate
```

---

# Troubleshooting

## Docker is not running

Make sure Docker Desktop is running before starting the application analysis or load-testing workflow.

Verify:

```bash
docker --version
```

and:

```bash
docker info
```

---

## Python package errors

Make sure the virtual environment is activated:

```powershell
.\venv\Scripts\activate
```

Then reinstall dependencies:

```bash
pip install -r requirements.txt
```

---

## Port already in use

If the target application or Performance Predictor cannot start because a port is already occupied, identify and stop the process using that port or configure the application to use another available port.

---

## Repository already exists

The system uses an isolated workspace for cloned repositories. If a previous test has left an existing project directory, clean up the corresponding temporary workspace before retrying the analysis.

---

# Version History

## Version 1.0

Initial implementation of the Performance Predictor project.

Version 1 established the basic automated performance-testing workflow and initial application analysis functionality.

## Version 2.0

Finalized implementation with the integrated mathematical performance analysis pipeline, including:

* Automated repository analysis
* Framework and entry-point detection
* Docker-based application execution
* Health checking
* Route discovery
* Automated Locust load testing
* Runtime monitoring
* Performance metrics collection
* Universal Scalability Law
* Little's Law
* Queueing analysis
* Bottleneck analysis
* Capacity analysis
* Scalability prediction
* Evidence-based recommendations
* Integrated frontend dashboard

---

# Limitations

Performance prediction depends on the quality and range of the measured load-test data.

In particular:

* Mathematical models are fitted to observed performance measurements.
* Predictions far beyond the tested load range should be treated as extrapolations.
* Poor model fit reduces prediction confidence.
* Framework and entry-point detection depends on the structure of the target repository.
* Runtime resource measurements depend on the execution environment.
* Capacity estimates represent the tested environment and workload rather than a universal capacity of the application.
* Different workloads, hardware configurations, databases, networks, and deployment environments can produce different results.

Therefore, the system reports prediction confidence and supporting measurements rather than treating mathematical extrapolation as guaranteed behavior.

---

# Purpose

Performance Predictor was developed as a final-year engineering project to demonstrate how automated performance testing can be combined with mathematical performance models to analyze scalability and estimate practical capacity of web applications.

The project emphasizes:

```text
Measurement
    +
Mathematical Modeling
    +
Performance Analysis
    +
Engineering Recommendations
```

rather than relying solely on machine-learning-based prediction.
