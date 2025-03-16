# -*- coding: utf-8 -*-
"""Untitled20.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1RB1DM5dD37E2k0ZYy8BqM9TWRmHCm898
"""

# Import required libraries
import feedparser
import torch
import clip
import numpy as np
import urllib.parse
import requests
from PIL import Image
import io
import praw
from collections import defaultdict
import time
import gradio as gr
import pandas as pd
from datetime import datetime
import concurrent.futures
from bs4 import BeautifulSoup
import os
import glob

# Set API credentials
REDDIT_CLIENT_ID = 'y0FSKCBS75sA6w6OwF6dTg'
REDDIT_CLIENT_SECRET = 'NVYSgTomXmyZJfZqNTDrWh8q_D4StQ'
REDDIT_USER_AGENT = 'my_app/1.0'
IMGUR_CLIENT_ID = '7d73dc3fe376391'
UNSPLASH_ACCESS_KEY = 'yNnSxkPZCzWJqDh2YlruyJd4IFfJTkOuHXRQFZK-Rc8'

# Store user engagement metrics with more detailed tracking
user_engagement = defaultdict(lambda: {'clicks': 0, 'time_spent': 0, 'shares': 0})
content_engagement = defaultdict(lambda: {'views': 0, 'likes': 0, 'comments': 0})

def get_latest_keywords_file():
    files = glob.glob('search_keywords*.xlsx')
    if not files:
        return 'search_keywords.xlsx'
    numbers = [int(f.replace('search_keywords', '').replace('.xlsx', '')) for f in files if f != 'search_keywords.xlsx']
    if not numbers:
        return 'search_keywords.xlsx'
    return f'search_keywords{max(numbers)}.xlsx'

# Initialize keyword tracking DataFrame with auto-sorting functionality
KEYWORDS_FILE = get_latest_keywords_file()
if os.path.exists(KEYWORDS_FILE):
    keywords_df = pd.read_excel(KEYWORDS_FILE)
    keywords_df = keywords_df.sort_values('frequency', ascending=False)
else:
    keywords_df = pd.DataFrame(columns=['keyword', 'frequency', 'last_used'])

def get_top_keywords():
    if keywords_df.empty:
        return []
    return keywords_df.head(2)['keyword'].tolist()

def update_keywords_tracking(keywords):
    global keywords_df
    current_time = datetime.now()

    for keyword in keywords:
        keyword = keyword.strip().lower()
        if keyword in keywords_df['keyword'].values:
            mask = keywords_df['keyword'] == keyword
            keywords_df.loc[mask, 'frequency'] += 1
            keywords_df.loc[mask, 'last_used'] = current_time
        else:
            new_row = pd.DataFrame({
                'keyword': [keyword],
                'frequency': [1],
                'last_used': [current_time]
            })
            keywords_df = pd.concat([keywords_df, new_row], ignore_index=True)

    keywords_df = keywords_df.sort_values('frequency', ascending=False)
    keywords_df.to_excel(KEYWORDS_FILE, index=False)
    return keywords_df.head(2)

# Initialize CLIP model with higher precision
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)
model.eval()

embedding_cache = {}

def get_text_embedding(text):
    if text in embedding_cache:
        return embedding_cache[text]
    try:
        text = text.strip().lower()
        tokens = clip.tokenize([text[:77]]).to(device)
        with torch.no_grad():
            embedding = model.encode_text(tokens)
            embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        embedding_cache[text] = embedding.cpu().numpy()
        return embedding_cache[text]
    except Exception as e:
        print(f"Text embedding error: {e}")
        return None

def get_image_embedding(image_url):
    if image_url in embedding_cache:
        return embedding_cache[image_url]
    try:
        response = requests.get(image_url, timeout=5)
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        image_input = preprocess(image).unsqueeze(0).to(device)
        with torch.no_grad():
            embedding = model.encode_image(image_input)
            embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        embedding_cache[image_url] = embedding.cpu().numpy()
        return embedding_cache[image_url]
    except Exception as e:
        print(f"Image embedding error: {e}")
        return None

def extract_images_from_content(html_content):
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        img_tags = soup.find_all('img')
        return [img['src'] for img in img_tags if 'src' in img.attrs]
    except:
        return []

def fetch_medium(keywords, num=10):
    articles = []
    for keyword in keywords:
        try:
            encoded_keyword = urllib.parse.quote(keyword)
            feed = feedparser.parse(f"https://medium.com/feed/tag/{encoded_keyword}")
            for entry in feed.entries[:num]:
                if hasattr(entry, 'title') and hasattr(entry, 'link'):
                    article_images = []
                    if hasattr(entry, 'content'):
                        article_images = extract_images_from_content(entry.content[0].value)
                    elif hasattr(entry, 'description'):
                        article_images = extract_images_from_content(entry.description)

                    article_images = [img for img in article_images if img.startswith('http')]

                    articles.append({
                        "type": "Medium",
                        "title": entry.title,
                        "description": entry.description[:200] if hasattr(entry, 'description') else "",
                        "url": entry.link,
                        "keyword": keyword,
                        "timestamp": entry.published if hasattr(entry, 'published') else None,
                        "images": article_images
                    })
        except Exception as e:
            print(f"Medium fetch error: {e}")
            continue
    return articles

def fetch_unsplash(keywords, num=10):
    images = []
    for keyword in keywords:
        try:
            url = "https://api.unsplash.com/search/photos"
            headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
            params = {
                "query": keyword,
                "per_page": num,
                "order_by": "relevant"
            }
            response = requests.get(url, headers=headers, params=params, timeout=5)
            if response.status_code == 200:
                for img in response.json()['results']:
                    if 'urls' in img and 'regular' in img['urls']:
                        images.append({
                            "type": "Unsplash",
                            "title": img.get('description', '') or img.get('alt_description', ''),
                            "url": img['urls']['regular'],
                            "keyword": keyword,
                            "image": img['urls']['regular'],
                            "thumbnail": img['urls']['regular'],
                            "likes": img.get('likes', 0)
                        })
        except Exception as e:
            print(f"Unsplash fetch error: {e}")
            continue
    return images

def fetch_all_content(keywords, trending_keywords=[]):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        all_keywords = keywords[:3] + trending_keywords[:2]  # Take first 3 user keywords and 2 trending keywords
        futures = []
        futures.append(executor.submit(fetch_medium, all_keywords))
        futures.append(executor.submit(fetch_unsplash, all_keywords))

        done, _ = concurrent.futures.wait(futures, timeout=15)

        all_content = []
        for future in done:
            try:
                result = future.result()
                if result:
                    all_content.extend(result)
            except Exception as e:
                print(f"Content fetch error: {e}")
                continue

    return all_content

def rank_content(content_items, keywords):
    if not content_items:
        return []

    keyword_embeddings = []
    for k in keywords:
        emb = get_text_embedding(k)
        if emb is not None:
            keyword_embeddings.append(emb)

    if not keyword_embeddings:
        return content_items

    avg_keyword_embedding = np.mean(keyword_embeddings, axis=0)

    for item in content_items:
        if item['type'] == 'Medium':
            text = f"{item['title']} {item['description'][:200]}"
            embedding = get_text_embedding(text)
            item['embedding'] = embedding
        else:
            embedding = get_image_embedding(item.get('image', item.get('url')))
            item['embedding'] = embedding

        if embedding is not None:
            semantic_score = np.dot(avg_keyword_embedding, embedding.T)
            engagement_score = 0
            if item['type'] == 'Unsplash':
                engagement_score = item.get('likes', 0)
            item['score'] = 0.7 * semantic_score.item() + 0.3 * (engagement_score / 100)
        else:
            item['score'] = -np.inf

    return sorted(content_items, key=lambda x: x['score'], reverse=True)

class ContentState:
    def __init__(self):
        self.current_page = 0
        self.content = []
        self.keywords = []
        self.trending_keywords = []

content_state = ContentState()

def create_content_card(item):
    if item['type'] == 'Medium':
        image_html = ""
        if item.get('images'):
            image_html = f'<img src="{item["images"][0]}" alt="Article image" style="width:100%; height:100vh; object-fit:cover;">'

        return f"""
        <div class="content-card">
            {image_html}
            <div class="content-text">
                <h2>{item['title']}</h2>
                <p>{item['description']}</p>
                <p class="keyword-tag">Keyword: {item['keyword']}</p>
                <a href="{item['url']}" target="_blank" class="read-more">Read More</a>
            </div>
        </div>
        """
    else:
        return f"""
        <div class="content-card">
            <img src="{item['url']}" alt="{item['title']}" style="width:100%; height:100vh; object-fit:cover;">
            <div class="content-text">
                <h2>{item['title']}</h2>
                <p class="keyword-tag">Keyword: {item['keyword']}</p>
                <a href="{item['url']}" target="_blank" class="view-image">View Full Image</a>
            </div>
        </div>
        """

def search_content(keywords, page=0):
    if not keywords:
        return "Please enter some interests."

    user_keywords = [k.strip() for k in keywords.split(',') if k.strip()][:3]  # Limit to first 3 keywords
    if not user_keywords:
        return "Please enter valid keywords."

    # Update keywords tracking and get trending keywords
    content_state.trending_keywords = update_keywords_tracking(user_keywords)['keyword'].tolist()[:2]  # Get top 2 trending
    content_state.keywords = user_keywords
    content_state.current_page = page

    # Display search info
    search_info = f"""
    <div class="search-info">
        <h3>Your Interests: {', '.join(user_keywords)}</h3>
        <h4>Trending Topics: {', '.join(content_state.trending_keywords)}</h4>
    </div>
    """

    # Fetch content for both user keywords and trending keywords
    all_content = fetch_all_content(user_keywords, content_state.trending_keywords)
    ranked_content = rank_content(all_content, user_keywords + content_state.trending_keywords)

    # Store all content for pagination
    content_state.content = ranked_content

    return search_info + display_content(page)

def display_content(page):
    items_per_page = 5
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page

    content_html = "<div class='content-container'>"

    current_items = content_state.content[start_idx:end_idx]
    for item in current_items:
        content_html += create_content_card(item)
    content_html += "</div>"

    if end_idx < len(content_state.content):
        content_html += """
        <div class="load-more-container">
            <button onclick="document.querySelector('#load-more-btn').click()" class="load-more-btn">
                Load More
            </button>
        </div>
        """

    return content_html

def load_more():
    content_state.current_page += 1
    return display_content(content_state.current_page)

def download_keywords():
    return KEYWORDS_FILE

# Enhanced Custom CSS for modern design
custom_css = """
body {
    background: linear-gradient(135deg, #1a1c2c 0%, #2a3c54 100%);
    color: #FFFFFF;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
}

.gradio-container {
    max-width: 1200px !important;
    margin: 0 auto;
}

.search-info {
    background: rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(10px);
    padding: 20px;
    margin: 20px 0;
    border-radius: 20px;
    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
    border: 1px solid rgba(255, 255, 255, 0.18);
}

.search-info h3, .search-info h4 {
    margin: 10px 0;
    color: #fff;
    text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
}

.content-container {
    display: grid;
    gap: 30px;
    padding: 20px;
}

.content-card {
    background: rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(10px);
    border-radius: 25px;
    height: 100vh;
    position: relative;
    overflow: hidden;
    transition: transform 0.3s ease;
    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
    border: 1px solid rgba(255, 255, 255, 0.18);
}

.content-card:hover {
    transform: translateY(-5px);
}

.content-text {
    padding: 30px;
    background: linear-gradient(to bottom, transparent, rgba(0, 0, 0, 0.9));
    position: absolute;
    bottom: 0;
    width: 100%;
}

input[type="text"] {
    background: rgba(255, 255, 255, 0.1) !important;
    border: 2px solid rgba(255, 255, 255, 0.2) !important;
    border-radius: 15px !important;
    color: white !important;
    padding: 15px !important;
    font-size: 16px !important;
    backdrop-filter: blur(10px);
}

button {
    background: linear-gradient(135deg, #8b2323 0%, #8b2323 100%) !important;
    border: none !important;
    border-radius: 15px !important;
    color: white !important;
    padding: 12px 25px !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 1px !important;
    transition: all 0.3s ease !important;
    box-shadow: 0 4px 15px rgba(139, 35, 35, 0.3) !important;
}

button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 6px 20px rgba(139, 35, 35, 0.4) !important;
}

.keyword-tag {
    display: inline-block;
    background: rgba(29, 161, 242, 0.2);
    color: #1DA1F2;
    padding: 5px 15px;
    border-radius: 20px;
    font-size: 14px;
    margin-top: 10px;
}

.load-more-container {
    text-align: center;
    margin: 30px 0;
    position: fixed;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 1000;
}

.load-more-btn {
    background: linear-gradient(135deg, #8b2323 0%, #8b2323 100%) !important;
    padding: 15px 40px !important;
    border-radius: 30px !important;
    font-size: 16px !important;
    font-weight: bold !important;
    text-transform: uppercase !important;
    letter-spacing: 2px !important;
    box-shadow: 0 4px 15px rgba(139, 35, 35, 0.3) !important;
}

.load-more-btn:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 6px 20px rgba(139, 35, 35, 0.4) !important;
}
"""

# Gradio interface
with gr.Blocks(css=custom_css) as iface:
    gr.Markdown("# AURA - Authentic Unified Relationship Algorithm")

    with gr.Row():
        keywords_input = gr.Textbox(
            lines=1,
            placeholder="Enter up to 3 interests (comma-separated)...",
            label="Your Interests"
        )
        search_btn = gr.Button("Explore", variant="primary")

    content_output = gr.HTML()
    load_more_btn = gr.Button("Load More", elem_id="load-more-btn", visible=False)
    download_btn = gr.Button("Download Keywords Data")

    search_btn.click(
        fn=search_content,
        inputs=[keywords_input],
        outputs=[content_output]
    )

    load_more_btn.click(
        fn=load_more,
        inputs=[],
        outputs=[content_output]
    )

    download_btn.click(
        fn=download_keywords,
        inputs=[],
        outputs=[gr.File()]
    )

if __name__ == "__main__":
    iface.launch()
