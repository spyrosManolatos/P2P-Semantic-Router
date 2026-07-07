import os
import csv
import json
import sys

# Ensure src/ is in the python path to import core
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config_loader

def main():
    config = config_loader.load_config()
    
    # Safely get paths from config (falling back to sensible defaults)
    try:
        raw_csv_path = config['storage']['data']['kaggle']['raw_path']
        normalized_path = config['storage']['data']['kaggle']['normalized_path']
    except KeyError:
        raw_csv_path = "data/raw/udemy_courses.csv"
        normalized_path = "data/raw/normalized_kaggle_courses.json"

    # Ensure output path is .json (in case config.yaml still has .csv)
    if normalized_path.endswith(".csv"):
        normalized_path = normalized_path.rsplit(".", 1)[0] + ".json"

    # Resolve absolute paths relative to project root
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    abs_raw_csv = os.path.join(project_root, raw_csv_path)
    abs_normalized = os.path.join(project_root, normalized_path)

    # If the raw path doesn't exist, try falling back to data/raw/udemy_courses.csv
    if not os.path.exists(abs_raw_csv):
        abs_raw_csv = os.path.join(project_root, "data", "raw", "udemy_courses.csv")
        if not os.path.exists(abs_raw_csv):
            print(f"Error: Could not find raw dataset at {abs_raw_csv}")
            return

    print(f"Reading Kaggle dataset from {abs_raw_csv}...")
    normalized_courses = []

    # udemy_courses.csv Schema: id,title,url,is_paid,instructor_names,category,headline,num_subscribers,rating,num_reviews,instructional_level,objectives,curriculum
    with open(abs_raw_csv, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Combine headline and objectives for a highly rich semantic description
            headline = row.get('headline', '').strip()
            objectives = row.get('objectives', '').strip()
            
            description = headline
            if objectives:
                description += f" Objectives: {objectives}"

            course = {
                "course_id": str(row.get('id', '')),
                "course_title": row.get('title', '').strip(),
                "description": description.strip(),
                "category": row.get('category', '').strip()
            }
            normalized_courses.append(course)

    print(f"Successfully parsed {len(normalized_courses)} courses.")
    print(f"Saving to {abs_normalized}...")
    
    os.makedirs(os.path.dirname(abs_normalized), exist_ok=True)
    with open(abs_normalized, 'w', encoding='utf-8') as f:
        json.dump(normalized_courses, f, indent=4)
        
    print("Done! The dataset is ready for K-Means training and DHT injection.")

if __name__ == "__main__":
    main()
