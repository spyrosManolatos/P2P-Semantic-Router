import json
import os
import uuid
import random
import argparse
from typing import List, Dict, Any
from faker import Faker

# Define 5 categories enum
CATEGORIES = [
    "Computer Science",
    "Mathematics",
    "Business",
    "Humanities",
    "Sciences"
]

# Academic course subjects mapping to categories to generate realistic titles
SUBJECTS = {
    "Computer Science": [
        "Machine Learning", "Algorithms", "Software Engineering", "Database Systems",
        "Web Development", "Cloud Computing", "Artificial Intelligence", "Cybersecurity",
        "Data Structures", "Human-Computer Interaction", "Computer Networks", "Mobile App Development"
    ],
    "Mathematics": [
        "Calculus I", "Calculus II", "Linear Algebra", "Discrete Mathematics",
        "Probability and Statistics", "Abstract Algebra", "Differential Equations", "Real Analysis",
        "Numerical Methods", "Graph Theory", "Number Theory", "Topology"
    ],
    "Business": [
        "Microeconomics", "Macroeconomics", "Financial Accounting", "Marketing Principles",
        "Corporate Finance", "Organizational Behavior", "Business Ethics", "Strategic Management",
        "Entrepreneurship", "Operations Management", "International Business", "Supply Chain Management"
    ],
    "Humanities": [
        "Introduction to Philosophy", "World History", "Creative Writing", "Modern Literature",
        "History of Art", "Cultural Anthropology", "Ethics in the Modern World", "Comparative Religions",
        "Classical Mythology", "Sociology", "Political Science", "Media Studies"
    ],
    "Sciences": [
        "General Chemistry", "Organic Chemistry", "Introduction to Physics", "Cell Biology",
        "Environmental Science", "Astronomy", "Genetics", "Geology",
        "Marine Biology", "Thermodynamics", "Neuroscience", "Ecology"
    ]
}

PREFIXES = ["Introduction to", "Advanced", "Foundations of", "Principles of", "Applied", "Topics in", "Methods of"]

def generate_course_title(category: str, fake: Faker) -> str:
    """Generates a realistic course title based on the category."""
    subjects = SUBJECTS.get(category, ["General Studies"])
    subject = random.choice(subjects)
    
    # 40% chance of adding a prefix to the subject
    if random.random() < 0.4:
        prefix = random.choice(PREFIXES)
        # Avoid double 'Introduction to ...' if the subject already has it
        if not subject.startswith("Introduction to") and not subject.startswith("General"):
            return f"{prefix} {subject}"
    return subject

def generate_fake_courses(num_courses: int) -> List[Dict[str, Any]]:
    """Generates a list of fake courses conforming to the schema."""
    fake = Faker()
    courses = []
    
    for _ in range(num_courses):
        category = random.choice(CATEGORIES)
        course_title = generate_course_title(category, fake)
        
        course = {
            "course_id": str(uuid.uuid4()),
            "course_title": course_title,
            "description": fake.paragraph(nb_sentences=random.randint(2, 4)),
            "category": category
        }
        courses.append(course)
        
    return courses

def main():
    parser = argparse.ArgumentParser(description="Generate synthetic course data.")
    parser.add_argument(
        "--num-courses",
        type=int,
        default=20,
        help="Number of fake courses to generate (default: 20)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=os.path.join(os.path.dirname(__file__), "data.json"),
        help="Path to the JSON file where generated data will be saved"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to existing data if the file already exists instead of overwriting"
    )
    
    args = parser.parse_args()
    
    # Generate new mock courses
    new_courses = generate_fake_courses(args.num_courses)
    
    # Load existing courses if append is selected and file exists
    courses_to_save = []
    if args.append and os.path.exists(args.output):
        try:
            with open(args.output, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    courses_to_save = json.loads(content)
                    if not isinstance(courses_to_save, list):
                        print(f"Warning: {args.output} does not contain a list. Overwriting instead.")
                        courses_to_save = []
        except Exception as e:
            print(f"Error reading existing file {args.output}: {e}. Overwriting instead.")
    
    courses_to_save.extend(new_courses)
    
    # Write to target file
    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(courses_to_save, f, indent=4)
        print(f"Successfully generated {args.num_courses} courses and saved to {args.output}.")
        print(f"Total courses in file: {len(courses_to_save)}.")
    except Exception as e:
        print(f"Error writing to file {args.output}: {e}")

if __name__ == "__main__":
    main()
