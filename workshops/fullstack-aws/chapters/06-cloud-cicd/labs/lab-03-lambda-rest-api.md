# Lab: Lambda REST API with API Gateway

## What You'll Build

A serverless REST API backed by Lambda: no servers, no nginx, no EC2.
API Gateway receives HTTP requests and invokes your Lambda function.

```
Browser / curl → API Gateway → Lambda Function → JSON response
```

You will expose two endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/items` | Return a list of items |
| POST | `/items` | Accept a new item and echo it back |

---

## Prerequisites

- Completed [Lab: Lambda S3 Trigger](./lab-02-lambda-s3-trigger.md)
- Region set to **us-east-1**

---

## Part 1: Create the Lambda Function

1. Go to **Lambda** → **Create function**
2. Select **Author from scratch**
3. Fill in:
   - Function name: `student-<NAME>-rest-api`
   - Runtime: **Python 3.12**
   - Region: **us-east-1**
4. Click **Create function**

### Add the code

In the **Code** tab, replace the default code with:

```python
import json

# In-memory store (resets on cold start: good enough for the lab)
items = [
    {"id": 1, "name": "Laptop"},
    {"id": 2, "name": "Monitor"},
    {"id": 3, "name": "Keyboard"},
]

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path   = event.get("rawPath", "/")

    print(f"Request: {method} {path}")
    print(f"Event: {json.dumps(event)}")

    # GET /items
    if method == "GET" and path == "/items":
        return response(200, {"items": items})

    # POST /items
    if method == "POST" and path == "/items":
        body = json.loads(event.get("body") or "{}")
        name = body.get("name")

        if not name:
            return response(400, {"error": "Missing 'name' field"})

        new_item = {"id": len(items) + 1, "name": name}
        items.append(new_item)
        return response(201, {"created": new_item})

    # Fallback
    return response(404, {"error": f"Route not found: {method} {path}"})


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
```

Click **Deploy**.

---

## Part 2: Create the API Gateway

1. Go to **API Gateway** → **Create API**
2. Choose **HTTP API** → click **Build**
3. Click **Add integration**:
   - Integration type: **Lambda**
   - Lambda function: `student-rest-api`
   - Version: **2.0** (payload format)
4. API name: `student-rest-api-gw`
5. Click **Next**

### Configure routes

Add two routes:

| Method | Resource path |
|--------|--------------|
| GET | `/items` |
| POST | `/items` |

Click **Next** → **Next** → **Create**.

---

## Part 3: Get the API URL

1. In the left sidebar, click **Deploy** → **Stages**
2. Click the **`$default`** stage
3. Copy the **Invoke URL**: it looks like:
   ```
   https://abc123.execute-api.us-east-1.amazonaws.com
   ```

---

## Part 4: Test the API

Open a terminal and run:

### GET /items

```bash
curl https://<your-invoke-url>/items
```

Expected response:
```json
{
  "items": [
    {"id": 1, "name": "Laptop"},
    {"id": 2, "name": "Monitor"},
    {"id": 3, "name": "Keyboard"}
  ]
}
```

### POST /items

```bash
curl -X POST https://<your-invoke-url>/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Headphones"}'
```

Expected response:
```json
{
  "created": {"id": 4, "name": "Headphones"}
}
```

### Test missing field (error handling)

```bash
curl -X POST https://<your-invoke-url>/items \
  -H "Content-Type: application/json" \
  -d '{}'
```

Expected response:
```json
{"error": "Missing 'name' field"}
```

---

## Part 5: View Logs in CloudWatch

1. Go to **Lambda** → `student-rest-api` → **Monitor** tab
2. Click **View CloudWatch logs**
3. Open the latest log stream
4. You should see each request logged:
   ```
   Request: GET /items
   Request: POST /items
   ```

> Every `print()` call in your Lambda is a CloudWatch log entry: this is how you debug a serverless API.

---

## Part 6: Put CloudFront in Front of the API (Optional)


1. Go to **CloudFront** → **Create distribution**
2. Select Free plan
3. Give a distribution name ( ignore the route 53 domain warning) -> next
4. Origin type -> API gateway -> Select your API gateway
2. **Origin domain**: paste your API Gateway invoke URL's host, e.g.
   `abc123.execute-api.us-east-1.amazonaws.com` (host only, no `https://`)
3. **Origin path**: /items
4. Leave everything default and hit next
5.. Click **Create distribution**

Wait a few minutes for the distribution status to become **Enabled**, then
copy the **Distribution domain name** (looks like `d123abc456.cloudfront.net`).

Validate

```
curl https://d3gj5omi4eu3d4.cloudfront.net/items
{"items": [{"id": 1, "name": "Laptop"}, {"id": 2, "name": "Monitor"}, {"id": 3, "name": "Keyboard"}]}%
```

### Test through CloudFront

```bash
curl https://<your-distribution-domain>/items
```

```bash
curl -X POST https://<your-distribution-domain>/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Headphones"}'
```

Both should behave identically to hitting the `execute-api` URL directly in
Part 4.

### Why this matters for the capstone

The [Task Tracker capstone project](../../../projects/01-task-tracker/) uses
this exact pattern for its frontend: CloudFront in front of an S3 bucket for
static assets, and a **separate** direct API Gateway URL for API calls (no
CloudFront in front of the API there). Fronting the API with CloudFront here
is optional practice, not something the capstone's Terraform sets up for you.

---

## What You Learned

| Concept | What happened |
|---------|--------------|
| HTTP API Gateway | Receives HTTP requests and routes them to Lambda |
| Lambda as a web handler | One function handles all routes via `rawPath` and `method` |
| Payload format 2.0 | API Gateway sends a structured event with `requestContext.http.method` |
| JSON responses | Lambda returns `statusCode`, `headers`, and `body` |
| Serverless debugging | CloudWatch Logs captures all print output: no SSH needed |
| CloudFront in front of an API | Origin can be API Gateway, not just S3; caching must be handled deliberately |

---

## Cleanup

```
1. CloudFront → your distribution → Disable, wait for it to deploy, then Delete
2. API Gateway → student-rest-api-gw → Delete
3. Lambda → student-rest-api → Delete
```
