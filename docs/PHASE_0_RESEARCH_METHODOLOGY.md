# PHASE 0: Research Methodology & Experimental Design

**Project:** Automated Analytical Performance Evaluation System for Web Applications  
**Status:** FYP Research Design Document  
**Created:** September 2026  
**Version:** 1.0  

---

## Executive Summary

This document formalizes the research contribution, experimental design, and validation protocol for an automated system that integrates classical analytical performance models (Little's Law, Queueing Theory, USL, Amdahl's Law, Forced Flow) with repository analysis, load testing, runtime monitoring, and empirical prediction validation.

**Core Research Contribution:**  
Not "we invented new mathematical models" but rather **"we systematically integrated, automated, and empirically validated classical analytical performance models in an end-to-end pipeline that can predict web-application scalability, capacity, and bottlenecks without manual instrumentation."**

---

## Part 1: Research Questions & Hypotheses

### Research Question 1 (RQ1): Automation & Framework Independence

**RQ1: Can the system automatically analyze, deploy, and load-test diverse web applications without manual performance instrumentation?**

**Sub-Questions:**
- RQ1a: Can framework detection reliably identify application type, entry point, and dependencies from source code?
- RQ1b: Can Docker-based execution provide a reproducible testing environment across different technology stacks?
- RQ1c: Can route discovery automatically identify testable endpoints without manual specification?

**Hypothesis H1:**  
The system can successfully analyze and test applications across ≥4 different web frameworks (Flask, FastAPI, Express, Spring Boot) with >90% successful deployment and route discovery rate.

**Why This Matters:**  
Framework-independent automation is a prerequisite for your research. If you must manually instrument every application, the system fails its primary promise.

**Success Criteria:**
- ✓ Framework detection confidence score >0.85 for target frameworks
- ✓ Docker deployment success rate >95% for test applications
- ✓ Automatic route discovery discovers ≥80% of testable endpoints
- ✓ System produces reproducible results across 3+ runs on same application

**Baseline:** Existing system (if already partially working)

---

### Research Question 2 (RQ2): USL Prediction Accuracy on Unseen Workloads

**RQ2: Can the Universal Scalability Law (USL), when fitted on lower workloads, accurately predict throughput behavior at higher unseen workloads?**

**Sub-Questions:**
- RQ2a: What prediction accuracy (MAPE, RMSE) can USL achieve when extrapolating 2-4x beyond training data?
- RQ2b: How does prediction accuracy degrade as load increases beyond the model's training range?
- RQ2c: At what point does the USL model's assumptions break down (coherency delay dominates)?

**Hypothesis H2:**  
USL, fitted on workloads up to 500 concurrent users, can predict throughput at 600-1500 users with <15% Mean Absolute Percentage Error (MAPE).

**Theoretical Basis:**  
<cite index="11-1,12-1">The USL is a black-box model that doesn't require low-level service time measurements, instead modeling throughput behavior through contention (α) and coherency delay (β) parameters that subsume fundamental scaling limitations</cite>. Recent applications <cite index="6-1">demonstrate strong prediction accuracy with proper parameter tuning and validation methods</cite>.

**Why This Matters:**  
USL is your system's most powerful analytical model. If USL predictions are poor, your research claim weakens significantly. If they're accurate, you have strong evidence.

**Success Criteria:**
- ✓ MAPE on validation set (600-1500 users) ≤15%
- ✓ RMSE is stable or improving as load increases (within model's valid range)
- ✓ Model parameters (α, β) are statistically significant (p<0.05)
- ✓ R² goodness-of-fit ≥0.90 on fitting data
- ✓ Bootstrap 95% confidence intervals show meaningful uncertainty quantification

**Measurement Strategy:**  
Fit USL using concurrency 20, 50, 100, 200, 300, 400, 500.  
Predict at 600, 800, 1000, 1200, 1500 (or until application degradation).  
Validate by actually running those loads and comparing predicted vs. observed throughput.

---

### Research Question 3 (RQ3): Little's Law Empirical Validation

**RQ3: Does Little's Law accurately characterize the relationship between arrival rate, response time, and concurrency in real web applications under varying load?**

**Formula:**
$$L = \lambda W$$

Where:
- $L$ = average number of requests in the system (concurrency)
- $\lambda$ = throughput (requests/sec)
- $W$ = average response time (seconds)

**Hypothesis H3:**  
For each load level, predicted concurrency using Little's Law ($L = \lambda W$) will differ from observed concurrency by <5% across all tested applications.

**Theoretical Basis:**  
<cite index="14-1">Little's Law is fundamental to queueing theory and states that the average number of users/tasks in a system equals the product of arrival rate and time spent in system</cite>. <cite index="18-1,19-1">Recent research frameworks demonstrate that Little's Law can validate load testing accuracy by comparing predicted vs. observed concurrency</cite>.

**Why This Matters:**  
Little's Law is theoretically bulletproof IF your system is in steady state. Deviations reveal transient behavior, measurement errors, or system instability—all valuable insights.

**Success Criteria:**
- ✓ Prediction error <5% for ≥80% of load levels tested
- ✓ Errors are random (not systematically biased high/low)
- ✓ Error increases predictably at extreme loads (near saturation)
- ✓ Works consistently across all 4+ test applications

**Measurement Strategy:**

For each load level, calculate:
$$\text{Predicted } L = \lambda \times W$$
$$\text{Error} = \frac{|L_{\text{predicted}} - L_{\text{observed}}|}{L_{\text{observed}}} \times 100$$

Example table:

| Load | λ (req/s) | W (s) | Predicted L | Observed L | Error |
|-----:|----------:|------:|------------:|-----------:|------:|
| 100  | 20        | 0.20  | 4.0         | 4.2        | 4.8%  |
| 200  | 35        | 0.40  | 14.0        | 13.8       | 1.4%  |
| 500  | 50        | 1.00  | 50.0        | 51.0       | 2.0%  |

---

### Research Question 4 (RQ4): Bottleneck Identification Accuracy

**RQ4: Can runtime metrics combined with analytical models correctly identify the likely performance bottleneck in web applications with known resource constraints?**

**Hypothesis H4:**  
For 3 test applications with controlled bottleneck characteristics (CPU-heavy, DB-heavy, I/O-heavy), the system identifies the correct primary bottleneck with >85% accuracy.

**Theoretical Basis:**  
Bottleneck identification requires integrating multiple signals: <cite index="32-1,34-1">recent research shows that systematic bottleneck analysis requires combining resource monitoring with scenario-based load analysis to reduce post-deployment issues</cite>. <cite index="19-1">Forced Flow Law enables component-level demand analysis which is critical for database and cache bottleneck detection</cite>.

**Why This Matters:**  
Identifying bottlenecks is your system's most practical value. If developers can't trust the bottleneck diagnosis, the system is not useful.

**Success Criteria:**
- ✓ Correctly identifies CPU bottleneck in CPU-heavy app
- ✓ Correctly identifies DB bottleneck in DB-heavy app
- ✓ Correctly identifies I/O/external bottleneck in I/O-heavy app
- ✓ Reports evidence (not just a guess)
- ✓ Appropriate confidence/uncertainty in classification

**Test Applications:**

#### Application A: CPU-Heavy (Python Flask with computation)
```python
@app.route('/compute')
def compute():
    # Intensive numerical computation
    result = sum([i**2 for i in range(1000000)])
    return {'result': result}
```
Expected bottleneck: CPU

#### Application B: Database-Heavy (FastAPI with PostgreSQL)
```python
@app.get('/users')
async def get_users():
    # Multiple DB queries
    users = db.session.query(User).filter(...).all()
    orders = db.session.query(Order).filter(...).all()
    # Join operations
    return {'users': users, 'orders': orders}
```
Expected bottleneck: Database connection pool / query latency

#### Application C: I/O-Heavy (Express with external API calls)
```javascript
app.get('/fetch-data', async (req, res) => {
  const result1 = await externalAPI1.call();
  const result2 = await externalAPI2.call();
  const result3 = await externalAPI3.call();
  res.json({ data: [result1, result2, result3] });
});
```
Expected bottleneck: External API latency / network I/O

**Measurement Strategy:**

For each app, calculate:
- CPU utilization %
- Memory utilization %
- DB connection pool utilization %
- Response time
- Queueing delay (calculated from M/M/1 model)
- Component demand (from Forced Flow Law)

Bottleneck classification:

```
If CPU > 80%:
    Primary = CPU
Else if DB pool > 85%:
    Primary = Database
Else if Response_time HIGH and CPU/Memory LOW:
    Primary = External/I/O dependency
Else:
    Primary = Unknown
```

Compare classification vs. ground truth.

---

### Research Question 5 (RQ5): Integration Value – Ablation Study

**RQ5: Does combining multiple analytical models produce better capacity/bottleneck estimates than using runtime metrics alone or individual models in isolation?**

**Hypothesis H5:**  
The full analytical system (Little's Law + Queueing + USL + Bottleneck + Forced Flow) produces capacity recommendations that differ from runtime-only analysis by ≥10%, and these recommendations more accurately reflect actual system saturation point.

**Why This Matters:**  
This is your **core research contribution**. It proves that integration is valuable, not just an engineering exercise.

**Success Criteria:**
- ✓ Full system capacity prediction ≤10% error
- ✓ Runtime-only baseline has >20% error or missing context
- ✓ Ablation components show progressive improvement
- ✓ Recommendations are actionable (specific, evidence-based)

**Experimental Design:**

Run capacity analysis in 4 configurations:

**Config A: Runtime Metrics Only**
```
Input: CPU, Memory, Response time, Throughput
Output: "Current saturation ~60% threshold"
Method: Simple thresholds
```

**Config B: Runtime + Little's Law + Queueing**
```
Input: Concurrency, λ, W + M/M/1 model
Output: "Predicted saturation at 520 users, queue delay increasing"
Method: Classic queueing theory
```

**Config C: Full System (A + B + USL + Bottleneck)**
```
Input: All metrics + analytical models
Output: "Safe capacity 420 users. Bottleneck: DB pool. 
         Recommendation: Increase pool size before scaling."
Method: Integrated analysis
```

**Config D: Full System + Forced Flow (if DB data available)**
```
Input: Component demands, call graphs
Output: "Per-request DB load: 3 queries, ~45ms. 
         Redis: 1.2 calls, ~2ms. 
         External API risk identified."
Method: Component-level analysis
```

Compare:
- Capacity predictions accuracy
- Bottleneck identification correctness
- Usefulness of recommendations
- Confidence/uncertainty quantification

Example results table:

| Metric | Config A | Config B | Config C | Config D | Actual |
|--------|----------|----------|----------|----------|--------|
| Safe Capacity (users) | 450 | 430 | 420 | 425 | 420 |
| Bottleneck | "?" | "Queue" | "DB pool" | "DB pool + API" | DB pool (60%), API (40%) |
| Error | ±7% | ±2.4% | 0% | ±1.2% | baseline |
| Recommendation | "Scale CPU" | "More workers" | "Increase DB pool" | "Pool + cache API" | ✓ Actionable |

---

## Part 2: Experimental Design

### Test Applications

You need **4-5 diverse applications** for credible research. Not just 1 random GitHub project.

#### Application Set 1: Controlled Bottleneck Apps (described in RQ4)
- App A (CPU-heavy)
- App B (DB-heavy)
- App C (I/O-heavy)

#### Application Set 2: Real-World Examples
You need at least 1-2 open-source applications that represent realistic complexity:

**Option 1: Flask Blog/CMS**
- Example: Flask with PostgreSQL
- Realistic: DB queries, session management
- Expected: Mixed bottleneck characteristics

**Option 2: FastAPI REST API**
- Example: FastAPI with asyncio
- Realistic: High concurrency handling
- Expected: Different from Flask behavior

**Option 3: Express.js REST API**
- Example: Node.js with MySQL
- Realistic: JavaScript ecosystem, different async model
- Expected: Different concurrency characteristics

**Option 4: Spring Boot Application**
- Example: Java Spring with PostgreSQL
- Realistic: Enterprise framework, JVM behavior
- Expected: Different GC behavior, thread pool dynamics

### Load Test Matrix

#### Phase 1: Model Fitting (Dataset A)

Test at these concurrency levels:
```
20 concurrent users
50 concurrent users
100 concurrent users
200 concurrent users
300 concurrent users
400 concurrent users
500 concurrent users
```

**For each level:**
- Duration: 5 minutes (stable state)
- Repetitions: 3 runs minimum
- Spawn rate: Gradual (don't spike)
- Metrics: Throughput, latency (avg, p50, p95, p99), errors, CPU, memory

#### Phase 2: Model Validation (Dataset B)

Test at previously unseen levels:
```
600 concurrent users
800 concurrent users
1000 concurrent users
1200 concurrent users
1500 concurrent users
```

Stop early if:
- Error rate >5%
- Application becomes unstable
- System clearly saturated

**For each level:**
- Duration: 5 minutes (stable state)
- Repetitions: 3 runs minimum
- Metrics: Same as Phase 1

#### Phase 3: Bottleneck Validation

For each app, identify its saturation point:
```
Binary search for saturation:
- Start at 500 users
- If stable, increase by 50% → 750
- If unstable, decrease by 25% → next test
- Continue until saturation point identified
```

Record the **exact saturation point** (where error rate >5% or response time >acceptable SLO).

### Test Duration & Repetitions

**For fitting (Dataset A):**  
3 repetitions × 7 load levels × 5 min = ~175 min per app (~3 hours)

**For validation (Dataset B):**  
3 repetitions × 5 load levels × 5 min = ~125 min per app (~2 hours)

**For bottleneck discovery:**  
5-10 iterations × 3 reps × 5 min = ~150 min per app (~2.5 hours)

**Total per app: ~8-10 hours real time**  
**Total for 4 apps: 32-40 hours**  
(Can parallelize if running Docker instances on separate hardware)

---

## Part 3: Metrics & Measurements

### Primary Metrics (Required)

#### Load Testing Metrics
```
Concurrency:         Number of concurrent users
Throughput (λ):      Requests per second
Response Time (W):   Average response time (seconds)
P50, P95, P99:       Percentile latencies
Error Rate:          % of failed requests
```

#### Resource Metrics
```
CPU:                 % utilization (app container)
Memory:              % utilization (app container)
Network:             Bytes in/out
Disk I/O (if DB):    Read/write ops
```

#### Derived Metrics (Calculated)
```
Concurrency (L):     λ × W (Little's Law)
Utilization (ρ):     λ / μ (M/M/1 model)
Queue Delay (Wq):    ρ / (μ(1-ρ)) (M/M/1)
USL Fit:             α, β, γ parameters
Saturation Factor:   Current throughput / Peak throughput
```

### Optional but Valuable Metrics

If your test applications have instrumentation:
```
Database Query Count:       Queries per request
Database Query Latency:     Time in database per request
Redis Operations:           Cache hits/misses
External API Calls:         Calls per request and their latency
```

### Metric Collection Strategy

#### Automated Collection
```
Locust provides:
  - Request latencies
  - Throughput
  - Error rates
  - CSV export of results
  
Docker stats provides:
  - CPU %
  - Memory %
  - Network I/O
  
Custom monitoring script provides:
  - Timestamp-aligned metrics
  - Per-request measurements
  - Clean JSON output for analysis
```

#### Data Format (Reproducibility)
Store as JSON:
```json
{
  "experiment_id": "EXP_001",
  "app": "flask_simple",
  "concurrency": 100,
  "run_number": 1,
  "start_time": "2026-09-10T14:30:00Z",
  "duration_seconds": 300,
  "metrics": {
    "throughput_rps": 45.2,
    "response_time_mean_ms": 2213,
    "response_time_p95_ms": 3456,
    "response_time_p99_ms": 4200,
    "error_rate": 0.002,
    "cpu_percent": 62.3,
    "memory_mb": 256
  },
  "raw_data": "locust_results/EXP_001_run1.csv"
}
```

---

## Part 4: Validation Protocols

### Little's Law Validation

For each load level and run:

1. Calculate predicted concurrency:
   $$L_{\text{predicted}} = \lambda \times W$$

2. Measure actual concurrency (from Locust)

3. Calculate error:
   $$\text{MAPE} = \frac{|L_{\text{predicted}} - L_{\text{observed}}|}{L_{\text{observed}}} \times 100$$

4. Accept if error <5% for ≥80% of tests

### Queueing Model Validation

For M/M/1 model:

1. Measure arrival rate λ and service time (1/μ)
2. Calculate utilization: $\rho = \lambda / \mu$
3. Predict queue delay: $W_q = \frac{\rho}{\mu(1-\rho)}$
4. Compare with observed response time - think time
5. Accept if within 20% for ≥70% of tests

### USL Prediction Validation

**Training phase:**
1. Fit USL using Dataset A (20-500 users)
2. Obtain α, β, γ parameters
3. Calculate R² (should be >0.90)

**Validation phase:**
1. Predict throughput for Dataset B (600-1500 users) using fitted model
2. Actually run those load tests
3. Compare predicted vs. actual throughput

**Metrics:**
```
MAPE = Mean Absolute Percentage Error
RMSE = Root Mean Squared Error
R² = Coefficient of determination
95% CI = Bootstrap confidence interval
```

**Acceptance criteria:**
- MAPE ≤15% 
- RMSE ≤5 req/s
- 95% CI doesn't include zero
- Predictions improve progressively (not degrading)

### Bottleneck Identification Validation

**Setup:**
1. Choose 3 apps with known/controlled bottlenecks
2. Run system on each
3. Compare identified vs. expected bottleneck

**Classification matrix:**
```
               Predicted
             CPU  DB  I/O  Other
Actual CPU    TP   FN  FN   FN
       DB     FN   TP  FN   FN
       I/O    FN   FN  TP   FN
       Other  FN   FN  FN   TP
```

**Metrics:**
- Accuracy = (TP + TN) / Total
- Precision = TP / (TP + FP)
- Recall = TP / (TP + FN)

**Acceptance:** Accuracy >85% for primary bottleneck

---

## Part 5: Statistical Rigor

### Repeated Experiments & Confidence Intervals

**For each important load level, run 3 repetitions minimum:**

Example: 100 concurrent users, 3 runs
```
Run 1: Throughput = 45.2 req/s
Run 2: Throughput = 46.1 req/s
Run 3: Throughput = 44.9 req/s

Mean = 45.4 req/s
Std Dev = 0.6 req/s
95% CI = [44.2, 46.6] req/s
```

### Bootstrap Confidence Intervals

<cite index="50-1,56-1">Bootstrap methods provide non-parametric confidence intervals without assuming data distribution</cite>. For USL parameters:

1. Resample fitting data (with replacement) 1000 times
2. Fit USL to each bootstrap sample
3. Collect α, β values from each fit
4. Calculate percentiles (2.5%, 97.5%) → 95% CI

```python
# Pseudo-code
results = []
for i in range(1000):
    bootstrap_sample = resample(fitting_data)
    alpha, beta = fit_USL(bootstrap_sample)
    results.append((alpha, beta))

ci_alpha = percentile(results.alpha, [2.5, 97.5])
ci_beta = percentile(results.beta, [2.5, 97.5])
```

### Statistical Tests

When comparing configurations (ablation study):
- Use paired t-tests for same-app comparisons
- Use ANOVA for multi-app comparisons
- Report p-values and effect sizes

Example:
```
Config A capacity: [450, 450, 452] users (mean 450.7)
Config C capacity: [420, 418, 422] users (mean 420.0)

Paired t-test: t(2) = 8.45, p = 0.021 *
Effect size: d = 1.8 (large)

Conclusion: Config C significantly more accurate
```

---

## Part 6: Reproducibility & Documentation

### Experiment Logging

Every experiment must be logged with:

```
experiments/
  EXP_001/
    metadata.json           # Experiment settings
    locust_results.csv      # Raw Locust output
    runtime_metrics.json    # CPU/Memory/Network
    model_output.json       # Mathematical model results
    predictions.json        # USL predictions vs actual
    report.md              # Summary analysis
    
  EXP_002/
    ...
```

**metadata.json format:**
```json
{
  "experiment_id": "EXP_001",
  "timestamp": "2026-09-10T14:30:00Z",
  "application": "flask_simple",
  "application_repo": "https://github.com/.../flask_simple",
  "application_commit": "abc123def456",
  "concurrency": 100,
  "duration_seconds": 300,
  "spawn_rate": 10,
  "target_url": "http://localhost:5000",
  "docker_image": "sha256:...",
  "python_version": "3.11",
  "locust_version": "2.24",
  "system": {
    "cpu_cores": 4,
    "memory_gb": 8,
    "os": "Ubuntu 24.04"
  },
  "repetition": 1,
  "phase": "fitting"
}
```

### Code & Reproducibility

Every experiment must be reproducible from code:

```bash
# Exact command to reproduce EXP_001
python -m scripts.run_experiment \
  --experiment_id EXP_001 \
  --app flask_simple \
  --concurrency 100 \
  --duration 300 \
  --repetitions 3 \
  --output_dir experiments/EXP_001
```

This command should:
1. Clone the application
2. Build Docker image
3. Start container
4. Run Locust with specified parameters
5. Collect metrics
6. Run mathematical models
7. Save results with metadata

**Key:** Version everything
- Application source (git commit SHA)
- Docker image (sha256 hash)
- Python/dependency versions
- Load testing script version
- Mathematical model code version

---

## Part 7: Timeline & Milestones

### Week 1: Setup & Baseline
- [ ] Set up 4 test applications locally
- [ ] Get Locust running
- [ ] Set up Docker infrastructure
- [ ] Run baseline load test on 1 app (sanity check)
- [ ] Deliverable: Baseline results + experiment template

### Week 2-3: Dataset A (Fitting)
- [ ] Run 20, 50, 100, 200, 300, 400, 500 concurrent users
- [ ] 3 reps each = 21 Locust runs per app
- [ ] Collect all metrics
- [ ] Fit mathematical models (Little's, Queueing, USL)
- [ ] Deliverable: Model parameters + fitting accuracy

### Week 4: Dataset B (Validation)
- [ ] Run 600, 800, 1000, 1200, 1500 users
- [ ] Validate USL predictions
- [ ] Calculate MAPE, RMSE, confidence intervals
- [ ] Deliverable: Prediction accuracy report

### Week 5: Bottleneck Validation
- [ ] Run saturation tests on 3 controlled apps
- [ ] Classify bottlenecks
- [ ] Measure accuracy
- [ ] Deliverable: Bottleneck identification report

### Week 6: Ablation Study
- [ ] Run same tests with 4 different configurations
- [ ] Compare results
- [ ] Statistical testing
- [ ] Deliverable: Ablation study report

### Week 7-8: Analysis & Paper
- [ ] Create figures and tables for paper
- [ ] Write results section
- [ ] Discuss limitations
- [ ] Create final report
- [ ] Deliverable: Research paper draft

---

## Part 8: Expected Outcomes & Success Definition

### Success Scenario (Publish-Quality Results)

```
RQ1: ✓ PASS - System successfully tests 4 frameworks
RQ2: ✓ PASS - USL MAPE <12% on validation set
RQ3: ✓ PASS - Little's Law error <4% across 80%+ tests
RQ4: ✓ PASS - Bottleneck identification >85% accuracy
RQ5: ✓ PASS - Full system outperforms individual components by 10%+
```

**Conclusion:**  
"Classical analytical performance models, when automatically integrated with load testing and runtime monitoring, provide actionable capacity and bottleneck estimates that outperform runtime metrics alone. Model predictions showed <15% error on unseen workloads."

### Partial Success Scenario (Still Publishable)

```
RQ1: ✓ PASS
RQ2: ~ PARTIAL - MAPE 18% (acceptable, discuss why)
RQ3: ✓ PASS
RQ4: ✓ PASS
RQ5: ~ PARTIAL - Slight improvement, not 10%
```

**Conclusion:**  
"USL extrapolation shows limitations beyond 1000 users, likely due to[reasons]. Little's Law and bottleneck identification remain reliable. Integration provides modest but consistent improvements for capacity planning."

### Failure Scenario (Pivot Needed)

```
RQ2: ✗ FAIL - MAPE >25%
RQ5: ✗ FAIL - No clear benefit over baseline
```

**Response:**  
This signals that USL alone isn't the right model for your applications, or assumptions don't hold. You would need to:
- Investigate why USL fails (GC pauses? Variable processing? Queuing?)
- Consider hybrid models or machine learning calibration
- Shift research question to "why traditional models fail"

---

## Part 9: Known Limitations & Scope

### Assumptions

1. **Steady-state systems** – Little's Law assumes stable system. Transient behavior ignored.
2. **Markovian arrivals** – Queueing model assumes Poisson arrivals. Real traffic may be bursty.
3. **Single-bottleneck focus** – Most applications have multiple contention points. Analysis identifies primary only.
4. **No external caching** – CloudFront, CDN not modeled.
5. **Fixed database schema** – Database analysis assumes static queries.
6. **Python/Node/Java only** – No Go, Rust, etc. (out of scope)

### What We're NOT Investigating

- ✗ ML-based performance prediction
- ✗ Distributed system complexity (microservices)
- ✗ Network-level optimization
- ✗ Chaos engineering resilience
- ✗ Cost optimization
- ✗ Geographic scaling/multi-region

### Scope Boundaries

**In Scope:**
- Single-region, monolithic or simple microservice apps
- Traditional HTTP/REST workloads
- Standard databases (PostgreSQL, MySQL, MongoDB)
- Caching layers (Redis)

**Out of Scope:**
- gRPC/Protocol Buffers
- Real-time streaming (WebSocket-heavy)
- GraphQL (complex query shapes)
- Kubernetes autoscaling behavior
- Distributed tracing

---

## Part 10: Research Contribution Statement

### What You're NOT Claiming

❌ "We invented new mathematical models"  
❌ "Our system has 100% prediction accuracy"  
❌ "One model fits all applications"  

### What You ARE Claiming

✅ **"We developed an automated, framework-independent system that integrates classical analytical performance models (Little's Law, Queueing Theory, USL, Amdahl's Law, Forced Flow Law) with repository analysis, controlled load testing, runtime monitoring, and empirical prediction validation. We demonstrate that this integration produces interpretable, actionable capacity and bottleneck recommendations, with <15% prediction error on unseen workloads. We identify where and why model assumptions break down in real applications, providing a foundation for hybrid approaches."**

### Paper Structure (Aligns with Experiments)

1. **Introduction** – Problem: existing tools measure what happened, don't predict what will
2. **Related Work** – Performance testing, analytical modeling, capacity planning
3. **Proposed System** – Architecture diagram (repository → automation → models → validation)
4. **Mathematical Foundation** – Formulas for Little's, Queueing, USL, Amdahl, Forced Flow
5. **Experimental Design** – RQ1-RQ5, test applications, metrics (THIS DOCUMENT)
6. **Implementation** – Key design decisions, framework detection, Docker, Locust
7. **Results** – Tables & graphs for each RQ
8. **Discussion** – Where models succeed, where they fail, why
9. **Limitations** – Steady-state assumptions, Markovian limits, single-bottleneck focus
10. **Conclusion** – Summary + future work

---

## Part 11: References & Best Practices

### Load Testing References

- <cite index="42-1,43-1">Locust 2.32+ is recommended for Python-based load testing with real-time monitoring</cite>
- <cite index="44-1,46-1">Best practices include distributed testing setup, custom load shapes, and integration with monitoring systems for resource correlation</cite>

### Mathematical Modeling References

- <cite index="11-1">USL provides predictive capacity planning without detailed service-time measurements</cite>
- <cite index="22-1,24-1">M/M/1 and M/G/1 queue models are validated approaches for web server performance modeling, especially with processor sharing discipline</cite>
- <cite index="9-1">USL subsumes Amdahl's Law as a special case and provides boundaries for scalability analysis</cite>

### Validation References

- <cite index="18-1,19-1">Little's Law-based frameworks validate load testing tool accuracy automatically</cite>
- <cite index="50-1,56-1">Bootstrap confidence intervals provide model-agnostic uncertainty quantification without parametric assumptions</cite>
- <cite index="52-1,54-1">Multiple bootstrap methods exist; moving-block bootstrap with 1000+ resamples is recommended for time-series data</cite>

---

## Approval & Sign-Off

**Researcher/Student:** [Your Name]  
**Supervisor:** [Advisor Name]  
**Date:** September 2026  

**Signature:**  
By signing below, you confirm that this research methodology has been reviewed and approved for experimental execution.

---

## Appendix A: Sample Experiment Output Structure

```
experiments/
├── EXP_001_flask_20users_run1/
│   ├── metadata.json
│   ├── locust_results.csv
│   ├── runtime_metrics.json
│   ├── model_results.json
│   └── locust_report.html
│
├── EXP_002_flask_50users_run1/
├── ...
├── EXP_021_flask_500users_run3/    # Fitting dataset complete
│
├── FITTING_REPORT_flask.md          # USL fit results, R², parameters
│
├── EXP_022_flask_600users_run1/
├── ...
├── EXP_031_flask_1500users_run3/    # Validation dataset complete
│
├── VALIDATION_REPORT_flask.md       # Prediction accuracy, MAPE, RMSE
├── BOTTLENECK_REPORT.md             # Classification results
├── ABLATION_REPORT.md               # Config A vs B vs C vs D
│
└── FINAL_ANALYSIS/
    ├── rq1_automation_results.json
    ├── rq2_usl_validation.json
    ├── rq3_littles_law_validation.json
    ├── rq4_bottleneck_validation.json
    ├── rq5_ablation_results.json
    └── paper_draft.md
```

---

## Appendix B: Quick Reference Checklist

### Before Starting Experiments

- [ ] All test applications cloned and tested locally
- [ ] Docker environment set up and working
- [ ] Locust installation verified
- [ ] Monitoring scripts ready
- [ ] Experiment logging structure created
- [ ] Random seed fixed for reproducibility
- [ ] Network isolated (no background traffic)
- [ ] Disk space available (large CSV files)

### During Each Experiment

- [ ] Metadata recorded before run
- [ ] System in clean state (cache cleared)
- [ ] Monitor for errors in Locust
- [ ] Verify metrics collected successfully
- [ ] Save all raw data

### After Each Experiment

- [ ] Verify CSV files not corrupted
- [ ] Calculate summary statistics
- [ ] Compare to previous runs (same concurrency)
- [ ] Flag anomalies for investigation
- [ ] Version control all results

### At End of Phase

- [ ] Aggregate results across repetitions
- [ ] Calculate confidence intervals
- [ ] Fit models and validate
- [ ] Generate summary report
- [ ] Archive experiment folder

---

**End of Phase 0 Document**

---

## How to Use This Document

1. **First Read:** Understand the 5 research questions and why each matters
2. **Design Review:** Meet with your supervisor using this as the agenda
3. **Experiment Planning:** Use Part 7 (Timeline) to schedule work
4. **Execution:** Refer to Parts 2-6 during actual experiments
5. **Analysis:** Use success criteria in Part 8 to evaluate results
6. **Paper Writing:** Structure follows Part 10 (Research Contribution)

This document is **the contract between you and your research**. Deviations should be documented with reasons.
