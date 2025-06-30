import csv
import requests
from bs4 import BeautifulSoup
import time
from urllib.parse import quote_plus, urlparse
from datetime import datetime, timedelta
import re

def parse_date_posted(date_str):
    """Parses LinkedIn's relative date strings into datetime objects for sorting."""
    date_str = date_str.lower().strip()
    now = datetime.now()

    if date_str == 'n/a':
        return datetime.min

    try:
        if 'hour' in date_str:
            hours_ago = int(re.search(r'\d+', date_str).group())
            return now - timedelta(hours=hours_ago)
        elif 'day' in date_str:
            days_ago = int(re.search(r'\d+', date_str).group())
            return now - timedelta(days=days_ago)
        elif 'week' in date_str:
            weeks_ago = int(re.search(r'\d+', date_str).group())
            return now - timedelta(weeks=weeks_ago)
        elif 'month' in date_str:
            months_ago = int(re.search(r'\d+', date_str).group())
            return now - timedelta(days=30 * months_ago)
        elif 'year' in date_str:
            years_ago = int(re.search(r'\d+', date_str).group())
            return now - timedelta(days=365 * years_ago)
    except (ValueError, AttributeError):
        return now

    return now

def get_filter_choices():
    """Presents menus for all search filters and returns user's selections."""
    print("\n--- Optional Filters (press Enter to skip) ---")
    
    # Date Posted Filter
    print("\nFilter by Date Posted:")
    time_filters = { "1": "r86400", "2": "r604800", "3": "r2592000" }
    print("  1. Past 24 hours\n  2. Past Week\n  3. Past Month")
    f_TPR = time_filters.get(input("Enter number (e.g., 2): ").strip())

    # Experience Level Filter
    print("\nFilter by Experience Level:")
    exp_filters = { "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6" }
    print("  1. Internship\n  2. Entry level\n  3. Associate\n  4. Mid-Senior level\n  5. Director\n  6. Executive")
    choice = input("Enter numbers separated by commas (e.g., 2,3): ")
    selected_exp_codes = [exp_filters.get(c.strip()) for c in choice.split(',') if exp_filters.get(c.strip())]
    f_E = ",".join(selected_exp_codes) if selected_exp_codes else None

    # Job Type Filter
    print("\nFilter by Job Type:")
    job_type_filters = { "1": "F", "2": "P", "3": "C", "4": "T", "5": "I" }
    print("  1. Full-time\n  2. Part-time\n  3. Contract\n  4. Temporary\n  5. Internship")
    f_JT = job_type_filters.get(input("Enter number (e.g., 1): ").strip())

    # --- NEW: Workplace Filter ---
    # The f_WT parameter is used for this filter with values 1 (On-site), 2 (Remote), 3 (Hybrid) [4]
    print("\nFilter by Workplace:")
    workplace_filters = { "1": "1", "2": "2", "3": "3" }
    print("  1. On-site\n  2. Remote\n  3. Hybrid")
    f_WT = workplace_filters.get(input("Enter number (e.g., 2 for Remote): ").strip())
    
    return {"f_TPR": f_TPR, "f_E": f_E, "f_JT": f_JT, "f_WT": f_WT}

def run_linkedin_scraper():
    """Main function to run the complete, interactive LinkedIn job scraper."""
    keyword = input("Enter job keyword (e.g., AI Engineer): ")
    location = input("Enter location (e.g., Berlin): ")
    filters = get_filter_choices()

    print("\nStarting scraper with your selected criteria...")
    all_jobs_data = []
    page_number = 0

    while True:
        start_index = page_number * 25
        base_url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote_plus(keyword)}&location={quote_plus(location)}&start={start_index}"
        filter_params = "".join([f"&{key}={quote_plus(value)}" for key, value in filters.items() if value])
        full_url = base_url + filter_params

        print(f"Scraping Page {page_number + 1}...")
        try:
            response = requests.get(full_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            job_cards = soup.find_all('div', class_='base-card')
            if not job_cards:
                print("No more job listings found.")
                break
            for job in job_cards:
                try:
                    raw_link = job.find('a', class_='base-card__full-link')['href']
                    clean_link = raw_link.split('?')[0] # Remove tracking parameters
                    date_element = job.find('time', class_='job-search-card__listdate') or job.find('time')
                    all_jobs_data.append({
                        'Title': job.find('h3', class_='base-search-card__title').text.strip(),
                        'Company': job.find('h4', class_='base-search-card__subtitle').text.strip(),
                        'Location': job.find('span', class_='job-search-card__location').text.strip(),
                        'Date Posted': date_element.text.strip() if date_element else 'N/A',
                        'Link': clean_link
                    })
                except AttributeError:
                    continue
            page_number += 1
            time.sleep(1)
        except requests.exceptions.RequestException as e:
            print(f"An error occurred: {e}. Stopping scrape.")
            break

    if not all_jobs_data:
        print("\nScraping finished. No jobs found for your criteria.")
        return

    print(f"\nFound {len(all_jobs_data)} jobs. Sorting by most recent...")
    sorted_jobs = sorted(all_jobs_data, key=lambda job: parse_date_posted(job['Date Posted']), reverse=True)
    
    filename = f"linkedin_jobs_final_{keyword.replace(' ', '_')}.csv"
    try:
        fieldnames = ['Title', 'Company', 'Location', 'Date Posted', 'Link']
        with open(filename, 'w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_jobs)
        print(f"Scraping complete. All data saved to: '{filename}'")
    except IOError as e:
        print(f"Error writing to file: {e}")

if __name__ == "__main__":
    run_linkedin_scraper()
