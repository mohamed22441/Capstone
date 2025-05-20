from flask import Flask, render_template, request, redirect, url_for, flash, session
from google.cloud import storage
import os
import uuid
import time
from datetime import timedelta
import json

cache = {}

app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with a secure key

# Google Cloud Storage Configuration
UPLOAD_BUCKET_NAME = "uploadscsv"  # Replace with your upload bucket name
RESULTS_BUCKET_NAME = "outputscsv"  # Replace with your results bucket name


def generate_signed_url_v4(bucket_name, blob_name):
    """
    Generate a signed URL for a blob in Google Cloud Storage using v4 signing.
    Forces download by setting the Content-Disposition header.
    """
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=4),
        method="GET",
        response_disposition=f'attachment; filename="{blob_name.split("/")[-1]}"'  # Forces download
    )
    return url


os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C://Users//ninja//Desktop//GoogleCloud_Key//csv-processor-450404-0a63d6599c19.json"
# Initialize Google Cloud Storage Client
storage_client = storage.Client()

@app.route("/")
def home():
    cache.clear()  # Clear cache when returning home
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    file = request.files.get("file")
    if file:
        try:
            # Generate a unique filename ************
            filename = f"{uuid.uuid4()}_{file.filename}"

            # Upload the file directly to Google Cloud Storage
            upload_blob(file, filename)

            flash(f"File '{file.filename}' uploaded to Google Cloud Storage!", "success")
            return redirect(url_for("results", filename=filename))
        except Exception as e:
            flash(f"Failed to upload file: {str(e)}", "danger")
            return redirect(url_for("home"))
    else:
        flash("No file selected!", "danger")
        return redirect(url_for("home"))

def upload_blob(file, blob_name):
    try:
        bucket = storage_client.bucket(UPLOAD_BUCKET_NAME)
        blob = bucket.blob(blob_name)
        blob.upload_from_file(file)
        print(f"Uploaded {blob_name} to Google Cloud Storage.")
    except Exception as e:
        print(f"Error uploading {blob_name}: {str(e)}")
        raise

def get_fresh_client():
    return storage.Client()

@app.route("/results")
def results():
    try:
        filename = request.args.get("filename")
        if not filename:
            flash("No filename provided.", "danger")
            return redirect(url_for("home"))

        print(f"\n=== Entering results() for filename: {filename} ===")
        print(f"Current cache keys: {list(cache.keys())}")

        # Check cache with more thorough validation
        if filename in cache:
            cached_data = cache[filename]
            print(f"Found cache entry for {filename} with keys: {list(cached_data.keys())}")
            
            # Verify we have the minimum required data
            if "cleaned_csv" in cached_data and "summary" in cached_data:
                print("Cache hit with valid data")
                return render_template("results.html", **cached_data)
            else:
                print("Cache exists but missing required data")

        print("No valid cache found, processing from scratch...")

        # Initialize GCS client
        storage_client = get_fresh_client()
        bucket = storage_client.bucket(RESULTS_BUCKET_NAME)

        # Handle filename with/without cleaned_ prefix
        cleaned_filename = f"cleaned_{filename}" if not filename.startswith("cleaned_") else filename

        # Polling for cleaned CSV
        cleaned_csv = None
        csv_max_wait = 700
        csv_wait_interval = 2
        csv_elapsed_time = 0

        print(f"Waiting for cleaned CSV file: {cleaned_filename}")
        while csv_elapsed_time < csv_max_wait:
            cleaned_blob = bucket.blob(cleaned_filename)
            try:
                cleaned_blob.reload()
                if cleaned_blob.exists():
                    cleaned_csv = cleaned_filename
                    print(f"Found cleaned CSV file: {cleaned_csv}")
                    break
            except Exception as e:
                print(f"Error checking cleaned CSV: {e}")

            time.sleep(csv_wait_interval)
            csv_elapsed_time += csv_wait_interval

        if not cleaned_csv:
            flash("Cleaned CSV file not found after waiting.", "danger")
            return redirect(url_for("home"))

        # Extract base name for visualizations
        base_name = cleaned_csv.split("cleaned_", 1)[1].rsplit(".", 1)[0]
        print(f"Base name for visualizations: {base_name}")

        # Polling for the JSON metadata file
        json_blob_name = f"visualizations/{base_name}_visualization_metadata.json"
        json_blob = bucket.blob(json_blob_name)

        metadata_max_wait = 700  # Maximum time to wait (seconds)
        metadata_wait_interval = 2  # Check every 2 seconds
        metadata_elapsed_time = 0

        print("Waiting for visualization metadata to be available...")
        while metadata_elapsed_time < metadata_max_wait:
            try:
                json_blob.reload()  # Force metadata refresh
                if json_blob.exists():
                    print(f"Found visualization metadata: {json_blob_name}")
                    break
            except Exception as e:
                print(f"Error refreshing metadata blob: {e}")

            time.sleep(metadata_wait_interval)
            metadata_elapsed_time += metadata_wait_interval
            print(f"Checked... waiting {metadata_elapsed_time}/{metadata_max_wait} seconds")

        if not json_blob.exists():
            flash("Visualization metadata not found after waiting.", "danger")
            return redirect(url_for("home"))

        # Download and parse the JSON metadata
        metadata_json = json_blob.download_as_text()
        existing_visualizations = json.loads(metadata_json)

        # Print statements for each visualization
        for vis_name in existing_visualizations.keys():
            print(f"✅ Found {vis_name}")

        # Fetch the main summary file
        summary_blob_name = f"chatgpt_api/summary_{filename}.txt"
        summary_blob = bucket.blob(summary_blob_name)
        if summary_blob.exists():
            summary = summary_blob.download_as_text()
        else:
            summary = "Summary not found."

        # Fetch summaries only for existing visualizations
        visualization_summaries = {}
        signed_visualizations = {}
        for vis_name in existing_visualizations.keys():  # Only check summaries for existing visualizations
            summary_blob_name = f"chatgpt_api/summary_{vis_name}_{filename}.txt"
            summary_blob = bucket.blob(summary_blob_name)

            # Debug: Check if filename is correct
            print(f"🔍 Looking for {summary_blob_name}")

            start_time = time.time()
            found = False

            while time.time() - start_time < 110:  # Check for up to 110 seconds
                try:
                    summary_blob.reload()  # Force metadata refresh
                    if summary_blob.exists():
                        visualization_summaries[f"{vis_name}_summary"] = summary_blob.download_as_text()
                        print(f"✅ Found and downloaded {summary_blob_name}")
                        found = True
                        break  # Exit loop if found
                    else:
                        print(f"❌ Not found: {summary_blob_name}. Retrying...")
                        time.sleep(1)  # Wait 1 second before retrying
                except Exception as e:
                    time_until_summary = int(time.time() - start_time)
                    print(f"Still searching for.. {summary_blob_name}: Elapsed time: {time_until_summary} seconds")

            # If not found after 110 seconds
            if not found:
                print(f"🚨 110 seconds passed. {summary_blob_name} still missing.")
                visualization_summaries[f"{vis_name}_summary"] = "Summary not found. It might still be processing."
        for vis_name, vis_path in existing_visualizations.items():
            # Extract just the filename part from the full path
            blob_name = vis_path.replace(f"https://storage.googleapis.com/{RESULTS_BUCKET_NAME}/", "")
            # Convert %20 back to spaces for GCS lookup
            blob_name = blob_name.replace("%20", " ")
            signed_visualizations[vis_name] = generate_signed_url_v4(RESULTS_BUCKET_NAME, blob_name)

        # After processing, cache ALL data including original filename
        results_data = {
            "cleaned_csv": f"https://storage.googleapis.com/{RESULTS_BUCKET_NAME}/{cleaned_csv}",
            "cleaned_csv_signedURL": generate_signed_url_v4(RESULTS_BUCKET_NAME, cleaned_csv),
            **signed_visualizations,  # Use the signed URLs instead of direct links
            "summary": summary,
            **visualization_summaries,
            "original_filename": filename  # Crucial for consistent navigation
        }

        # Store in cache using both filename formats as keys
        cache[filename] = results_data
        if filename != cleaned_filename:
            cache[cleaned_filename] = results_data
            
        print(f"Cache updated for {filename} and {cleaned_filename}")
        print(f"Cache keys now: {list(cache.keys())}")
        
        return render_template("results.html", **results_data)

    except Exception as e:
        flash(f"Failed to fetch processed results: {str(e)}", "danger")
        return redirect(url_for("home"))
    

ml_visualization_cache = {}

@app.route("/check-ml-visualizations")
def check_ml_visualizations():
    try:
        filename = request.args.get("filename")
        if not filename:
            print("❌ No filename provided in check_ml_visualizations")
            return {"ml_plots_available": False}

        print(f"\n=== Starting ML Visualization Check ===")
        print(f"📁 Input filename: {filename}")

        # Check if the result is already cached
        if filename in ml_visualization_cache:
            print("✅ Cache hit for filename")
            return ml_visualization_cache[filename]

        # Initialize a fresh GCS client
        storage_client = get_fresh_client()
        bucket = storage_client.bucket(RESULTS_BUCKET_NAME)

        # Extract base name - keep the .csv part for ML files
        if filename.startswith("cleaned_"):
            base_name = filename[8:]  # Remove "cleaned_" prefix
            print(f"🔧 Removed 'cleaned_' prefix: {base_name}")
        else:
            base_name = filename
            print(f"🔧 No 'cleaned_' prefix found, using original: {base_name}")
        
        # # For ML files, we want to keep the .csv before _0.png
        # # So we only remove the final extension if it's not .csv
        # if not base_name.endswith('.csv'):
        #     base_name = base_name.rsplit(".", 1)[0]
        #     print(f"🔧 After removing extension: {base_name}")

        # Construct the patterns we're looking for in ML_predictions folder
        ml_patterns = [
            f"ML_predictions/linear_regression_{base_name}.png",
            f"ML_predictions/decision_tree_{base_name}.png",
            f"ML_predictions/feature_importance_{base_name}.png",
        ]

        # Construct the patterns for summary files
        summary_patterns = [
            f"chatgpt_api/lr_summary_{base_name}.txt",
            f"chatgpt_api/dt_summary_{base_name}.txt",
            f"chatgpt_api/rf_summary_{base_name}.txt",
        ]

        print("\n🔍 Checking for these exact files:")
        for pattern in ml_patterns + summary_patterns:
            print(f"  - {pattern}")

        # Check each file
        found_files = []
        found_summaries = []

        # Polling logic for both ml visualizations and summaries
        max_wait_time = 35  # Maximum wait time in seconds
        wait_interval = 1   # Check every second
        elapsed_time = 0

        while elapsed_time < max_wait_time:
            for pattern in ml_patterns:
                blob = bucket.blob(pattern)
                if blob.exists() and pattern not in found_files:
                    found_files.append(pattern)
                    print(f"✅ {pattern} EXISTS")

            for pattern in summary_patterns:
                blob = bucket.blob(pattern)
                if blob.exists() and pattern not in found_summaries:
                    found_summaries.append(pattern)
                    print(f"✅ {pattern} EXISTS")

            # If all files are found, break early
            if len(found_files) == len(ml_patterns) and len(found_summaries) == len(summary_patterns):
                break

            time.sleep(wait_interval)
            elapsed_time += wait_interval

        ml_plots_available = len(found_files) > 0 and len(found_summaries) > 0

        print("\n📊 Results:")
        print(f"ML Plots Available: {'YES' if ml_plots_available else 'NO'}")
        if found_files:
            print(f"Found files: {', '.join(found_files)}")
        if found_summaries:
            print(f"Found summaries: {', '.join(found_summaries)}")
        print("=== End of ML Visualization Check ===\n")

        # Cache the result
        result = {
            "ml_plots_available": ml_plots_available,
            "details": {
                "base_name": base_name,
                "checked_paths": ml_patterns + summary_patterns,
                "found_files": found_files,
                "found_summaries": found_summaries
            }
        }
        ml_visualization_cache[filename] = result

        return result

    except Exception as e:
        print(f"\n🔥 Error in check_ml_visualizations: {str(e)}")
        return {
            "ml_plots_available": False,
            "error": str(e)
        }
    
@app.route("/debug-cache")
def debug_cache():
    return json.dumps({k: list(v.keys()) if isinstance(v, dict) else str(v) for k, v in cache.items()})

# NEW ML page!
# Add this new route to handle summary downloads
@app.route("/download-summary/<filename>")
def download_summary(filename):
    try:
        summary_type = request.args.get('summary_type')
        if not summary_type:
            flash("No summary type specified", "danger")
            return redirect(url_for("machine_learning_page", filename=filename))

        # Determine the correct summary file name
        base_name = filename[8:] if filename.startswith("cleaned_") else filename
        base_name = base_name.rsplit(".", 1)[0] if not base_name.endswith('.csv') else base_name

        summary_files = {
            'lr': f"chatgpt_api/lr_summary_{base_name}.txt",
            'dt': f"chatgpt_api/dt_summary_{base_name}.txt",
            'rf': f"chatgpt_api/rf_summary_{base_name}.txt"
        }

        summary_path = summary_files.get(summary_type)
        if not summary_path:
            flash("Invalid summary type", "danger")
            return redirect(url_for("machine_learning_page", filename=filename))

        # Generate signed URL
        url = generate_signed_url_v4(RESULTS_BUCKET_NAME, summary_path)
        return redirect(url)

    except Exception as e:
        flash(f"Failed to generate download link: {str(e)}", "danger")
        return redirect(url_for("machine_learning_page", filename=filename))

# Update the machine_learning_page route
@app.route("/machinelearning-page")
def machine_learning_page():
    try:
        filename = request.args.get("filename")
        if not filename:
            flash("No filename provided.", "danger")
            return redirect(url_for("home"))

        print(f"\n=== DEBUG: ML Page Request ===")
        print(f"Input filename: {filename}")

        # Get base filename (handle cleaned_ prefix)
        base_name = filename[8:] if filename.startswith("cleaned_") else filename
        base_name = base_name.rsplit(".", 1)[0] if not base_name.endswith('.csv') else base_name

        # Initialize GCS client
        storage_client = get_fresh_client()
        bucket = storage_client.bucket(RESULTS_BUCKET_NAME)

        # Check for all possible ML visualizations
        ml_files = {
            'linear_regression': f"ML_predictions/linear_regression_{base_name}.png",
            'decision_tree': f"ML_predictions/decision_tree_{base_name}.png",
            'feature_importance': f"ML_predictions/feature_importance_{base_name}.png"
        }

        # Check for summaries
        summary_files = {
            'linear_regression': f"chatgpt_api/lr_summary_{base_name}.txt",
            'decision_tree': f"chatgpt_api/dt_summary_{base_name}.txt",
            'feature_importance': f"chatgpt_api/rf_summary_{base_name}.txt"
        }

        # Generate URLs for existing files
        urls = {}
        summaries = {}

        for name, path in ml_files.items():
            blob = bucket.blob(path)
            if blob.exists():
                urls[f"{name}_url"] = generate_signed_url_v4(RESULTS_BUCKET_NAME, path)

        for name, path in summary_files.items():
            blob = bucket.blob(path)
            if blob.exists():
                summaries[f"{name}_summary"] = blob.download_as_text()

        # Update cache
        cache_key = cache.get(filename, {}).get("original_filename", filename)
        if cache_key not in cache:
            cache[cache_key] = {}
        
        cache[cache_key].update({
            **urls,
            **summaries,
            "original_filename": filename
        })

        return render_template("machinelearning.html",
            filename=filename,
            linear_regression_url=urls.get('linear_regression_url'),
            decision_tree_url=urls.get('decision_tree_url'),
            feature_importance_url=urls.get('feature_importance_url'),
            linear_regression_summary=summaries.get('linear_regression_summary', "Summary not available"),
            decision_tree_summary=summaries.get('decision_tree_summary', "Summary not available"),
            feature_importance_summary=summaries.get('feature_importance_summary', "Summary not available")
        )

    except Exception as e:
        print(f"ERROR in machine_learning_page: {str(e)}")
        flash(f"Failed to load ML visualizations: {str(e)}", "danger")
        return redirect(url_for("results", filename=request.args.get("filename")))
    
@app.route("/download/<filename>")
def download_file(filename):
    try:
        url = generate_signed_url_v4(RESULTS_BUCKET_NAME, filename)
        return redirect(url)  # Redirects to the signed URL which now forces a download
    except Exception as e:
        flash(f"Failed to generate download link: {str(e)}", "danger")
        return redirect(url_for("results"))

@app.route("/visualization/<filename>")
def download_visualization(filename):
    try:
        # Generate a URL for the visualization
        bucket = storage_client.bucket(RESULTS_BUCKET_NAME)
        blob = bucket.blob(filename)
        url = blob.generate_signed_url_v4(expiration=14400)  # URL valid for 4 hours
        return redirect(url)
    except Exception as e:
        flash(f"Failed to generate visualization link: {str(e)}", "danger")
        return redirect(url_for("results"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)