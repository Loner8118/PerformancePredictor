# Experiment Report: EXP89293220

**Target Repository:** Performance-Testing-Demo  
**Target Commit:** None  
**Framework:** None  
**Created:** 2026-09-21T06:06:23+00:00  
**Tool Commit:** d68bb709faebbf772abbca757cfa02cffdd54909 (dirty working tree)


### Predicted vs. Actual Throughput

| Users | Predicted (req/s) | Actual (req/s) | Error (%) | Consistent |
|---|---|---|---|---|
| 600.0 | 198.4 | 191.69 | 3.5 | True |
| 700.0 | 187.89 | 163.398 | 14.99 | True |
| 800.0 | 177.99 | 66.08 | 169.36 | False |
| 1000.0 | 160.37 | 75.615 | 112.09 | False |
| 2000.0 | 105.26 | 193.571 | 45.62 | False |
| 3000.0 | 77.86 | 202.896 | 61.63 | False |
| 4000.0 | 61.7 | 225.416 | 72.63 | False |
| 5000.0 | 51.08 | 224.13 | 77.21 | False |
| 10000.0 | 27.42 | 213.402 | 87.15 | False |

Validated **9** load level(s): **2/9** consistent. MAE=107.11, RMSE=122.53, MAPE=71.58%.


### Little's Law: Predicted vs. Observed Concurrency

| Users | Predicted L | Observed Concurrency | Error (%) | Consistent |
|---|---|---|---|---|
| 600 | 14.599 | 600.0 | 97.57 | False |
| 700 | 21.066 | 700.0 | 96.99 | False |
| 800 | 31.896 | 800.0 | 96.01 | False |
| 1000 | 29.83 | 1000.0 | 97.02 | False |
| 2000 | 13.339 | 2000.0 | 99.33 | False |
| 3000 | 12.453 | 3000.0 | 99.58 | False |
| 4000 | 6.875 | 4000.0 | 99.83 | False |
| 5000 | 7.813 | 5000.0 | 99.84 | False |
| 10000 | 10.706 | 10000.0 | 99.89 | False |

MAE=2994.603, RMSE=4161.465, MAPE=98.45%.

### Queueing Model: Predicted vs. Observed Response Time

| Users | Predicted W (s) | Observed W (s) | Error (%) | Consistent |
|---|---|---|---|---|
| 600 | 0.024 | 0.076 | 69.08 | False |
| 700 | 0.015 | 0.129 | 88.65 | False |
| 800 | 0.006 | 0.483 | 98.7 | False |
| 1000 | 0.006 | 0.394 | 98.4 | False |
| 2000 | 0.025 | 0.069 | 63.8 | False |
| 3000 | 0.039 | 0.061 | 35.81 | False |
| 4000 | inf | 0.03 | — | False |
| 5000 | 0.539 | 0.035 | 1446.82 | False |
| 10000 | 0.06 | 0.05 | 18.62 | True |

MAE=0.201, RMSE=0.285, MAPE=239.98%.


### Ablation Study

Ground truth from validation load tests: safe capacity = **600.0 users**, bottleneck = **None**.

| Configuration | Safe Capacity | Capacity Error (%) | Identified Bottleneck | Bottleneck Correct |
|---|---|---|---|---|
| A_runtime_only | 987.91 | 64.65 | cpu_usage | — |
| B_plus_queueing | — | — | cpu_usage | — |
| C_full_analytical | 211.69 | 64.72 | cpu | — |
| D_plus_forced_flow | 211.69 | 64.72 | cpu | — |


### Available Analysis

- prediction validation
- Little's Law and Queueing validation
- ablation study
