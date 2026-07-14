# Cloud Workshops

Hands-on workshop labs built by [BeCloudReady](https://becloudready.com) for engineering teams. Each lab is self-contained, with pre-scoped IAM permissions, step-by-step walkthroughs, and sample data included. Drop into a workshop or run independently.

---

## Workshops

| Workshop | What you build | What you will be able to do | Stack |
|---|---|---|---|
| [`workshops/aws-data-lake/`](workshops/aws-data-lake/) | End-to-end data lake: raw ingestion, ETL, governance, CDC, and analytics | Design and operate the full AWS data engineering stack, from raw S3 files to a governed query layer in Athena and Redshift | S3, Glue, Athena, Lake Formation, Redshift, DMS, OpenSearch |
| [`workshops/aws-iam-policy/`](workshops/aws-iam-policy/) | Read, predict, and write IAM policies using a real sandbox policy as the textbook | Write least-privilege policies from scratch and audit existing ones without guessing | IAM, CloudShell |
| [`workshops/fullstack-aws/`](workshops/fullstack-aws/) | Full-stack app on AWS: React, FastAPI, MongoDB, Terraform, and CI/CD across 7 chapters and 4 deployable projects | Ship a production-ready app on AWS end-to-end, including infrastructure and automated deployment | React, FastAPI, Lambda, S3, DynamoDB, API Gateway, Terraform, GitHub Actions |
| [`workshops/equipment-inspection/`](workshops/equipment-inspection/) | Versioned inspection photo app for IoT / oil & gas equipment: presigned S3 upload, DynamoDB metadata, version history UI | Build internal tooling with photo capture, versioning, and audit trail on AWS | React, FastAPI, S3, DynamoDB, Terraform |
| [`workshops/databricks-db-agent-lakebase/`](workshops/databricks-db-agent-lakebase/) | Text-to-SQL agent backed by Lakebase (Postgres), Databricks Unity Catalog, and a self-hosted vLLM endpoint | Let non-technical stakeholders query the data warehouse in plain English, without writing SQL | Databricks, Delta Lake, vLLM |
| [`workshops/llmops/`](workshops/llmops/) | Deploy, observe, and route production LLM workloads on a GPU instance: vLLM serving, Prometheus/Grafana dashboards, and LiteLLM gateway with virtual keys and spend tracking | Run LLM inference in-house with full observability and cost controls, without depending on managed APIs | vLLM, LiteLLM, Prometheus, Grafana, DCGM, Docker, Ansible |

### Full-Stack on AWS: 7-chapter curriculum

| Chapter | Topic |
|---|---|
| 01 | Vibe Coding: FastAPI app with AI prompting |
| 02 | Backend & Databases: CRUD API + MongoDB |
| 03 | Testing: TDD cycle with AI assistance |
| 04 | Security: API key auth + JWT |
| 05 | Infrastructure: Terraform on AWS |
| 06 | Cloud & CI/CD: Lambda, API Gateway, S3, GitHub Actions |
| 07 | React: frontend, components, full-stack integration |

4 deployable projects: Task Tracker · Notice Board · URL Bookmark Saver · Architecture Diagram

---

### AWS Data Lake: 6-lab curriculum

Labs 1-3 build on each other. Labs 4-6 are standalone.

| Lab | Topic |
|---|---|
| Lab 1 | S3, Glue Crawler, Glue ETL (PySpark), Athena |
| Lab 2 | Event-driven ingestion: S3 to SQS to Lambda |
| Lab 3 | Data governance: Lake Formation row/column/tag-based access control |
| Lab 4 | Redshift Serverless, federated query from Aurora RDS |
| Lab 5 | Change Data Capture: Oracle/Postgres to DMS to S3 or Oracle/Postgres target |
| Lab 6 | OpenSearch: ingestion, search, and dashboards |

---

## About

[BeCloudReady](https://becloudready.com) is a Databricks Registered Partner that builds and delivers cloud workshops for engineering teams. We run community workshops at [TorontoAI](https://torontoai.io).

| | |
|---|---|
| **Cloud Workshops** | Per-student AWS / Azure / GCP / Databricks sandboxes, region-locked, namespace-scoped, teardown-clean |
| **AI & GPU Labs** | H100 / A100 cohorts on neo-cloud (Lambda Labs, Shadeform, RunPod): 30-70% cheaper than hyperscaler on-demand |
| **Sales Demo Environments** | Reproducible demo stacks for SE teams and partner programs |

**Need a workshop for your team?**
→ [becloudready.com/workshops](https://becloudready.com/workshops) · [Book a call](https://calendly.com/kchandank/30-mins-meeting)

---

## Tagging standard

Every resource created in a workshop must carry these tags. The nightly cleanup workflow reads them to decide what to delete.

| Tag | Example | Purpose |
|---|---|---|
| `workshop` | `aws-data-lake` | Which lab |
| `date` | `dd-mmm-yyy` | When the cohort ran 26-Jul-2026 |
| `autodelete` | `true` (default) | Set to `false` to protect a resource |

All Terraform modules in this repo apply these tags via `local.common_tags`. Resources created manually (via console or CLI) must be tagged manually.

**To protect a resource from nightly deletion**, add `AutoDelete = false`. Everything else tagged `Environment = workshop` is deleted at 3 AM EST.

See [`terraform/tags.tf`](terraform/tags.tf) for the shared locals block and [`tools/nightly-cleanup.py`](tools/nightly-cleanup.py) for the cleanup logic.

---

## Contributing

Found a bug or a gap in a published lab? Issues and PRs are welcome.
Have a lab that fits one of the tracks above? Reach out before opening a PR.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
