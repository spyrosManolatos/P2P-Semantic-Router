import time
from functools import wraps
from typing import List

def compute_recall(retrieved_ids: List[str], ground_truth_ids: List[str]) -> float:
    """
    Computes the Recall percentage.
    If ground truth expects [A, B, C, D, E] and we retrieved [A, X, C, Y, Z],
    Recall is 2/5 = 0.40 (40%).
    """
    if not ground_truth_ids: 
        return 0.0
    
    retrieved_set = set(retrieved_ids)
    gt_set = set(ground_truth_ids)
    
    intersection = retrieved_set.intersection(gt_set)
    return len(intersection) / len(gt_set)

def timer_decorator(func):
    """A decorator that measures execution time of a function in milliseconds."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        
        elapsed_ms = (end_time - start_time) * 1000.0
        
        return result, elapsed_ms
    return wrapper
