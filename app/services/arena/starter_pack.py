"""
A starter set of global questions and challenges, installed from the admin screen.

Idempotent: every row carries a `seed_key`, and a key already present is skipped, so pressing
the button twice (or after an admin edited a question) never duplicates or overwrites.
"""
from datetime import datetime

from app.core.firebase import firestore_arena_challenges, firestore_question_bank

# (category, difficulty, question, options, index of the correct option, explanation)
# An options value of None makes a TRUE_FALSE question, with the index 0 = True, 1 = False.
QUESTIONS = [
    # General knowledge
    ("GENERAL_KNOWLEDGE", 1, "How many days are there in a leap year?", ["365", "366", "364", "360"], 1, "A leap year adds 29 February, making 366 days."),
    ("GENERAL_KNOWLEDGE", 1, "How many continents are there on Earth?", ["5", "6", "7", "8"], 2, "Africa, Antarctica, Asia, Australia (Oceania), Europe, North America and South America."),
    ("GENERAL_KNOWLEDGE", 1, "Which is the national animal of India?", ["Lion", "Elephant", "Bengal tiger", "Peacock"], 2, "The Bengal tiger has been India's national animal since 1973."),
    ("GENERAL_KNOWLEDGE", 1, "Which colour do you get by mixing blue and yellow?", ["Green", "Purple", "Orange", "Brown"], 0, None),
    ("GENERAL_KNOWLEDGE", 2, "Who wrote India's national anthem, 'Jana Gana Mana'?", ["Bankim Chandra Chatterjee", "Rabindranath Tagore", "Sarojini Naidu", "Mahatma Gandhi"], 1, "Rabindranath Tagore wrote it; it was adopted as the anthem in 1950."),
    ("GENERAL_KNOWLEDGE", 2, "How many players are there in a football team on the field?", ["9", "10", "11", "12"], 2, None),
    ("GENERAL_KNOWLEDGE", 2, "Which is the largest ocean in the world?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3, "The Pacific covers about a third of Earth's surface."),
    ("GENERAL_KNOWLEDGE", 1, "A week has 7 days.", None, 0, None),
    # Science
    ("SCIENCE", 1, "Which planet is known as the Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1, "Iron oxide (rust) in its soil makes Mars look red."),
    ("SCIENCE", 1, "What do plants take in from the air to make food?", ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"], 2, "In photosynthesis plants use carbon dioxide, water and sunlight, and give out oxygen."),
    ("SCIENCE", 1, "What is the boiling point of water at sea level?", ["50 °C", "90 °C", "100 °C", "120 °C"], 2, None),
    ("SCIENCE", 1, "How many legs does an insect have?", ["4", "6", "8", "10"], 1, "Spiders have 8 legs, which is one way to tell they are not insects."),
    ("SCIENCE", 2, "Which gas makes up most of the air we breathe?", ["Oxygen", "Carbon dioxide", "Nitrogen", "Argon"], 2, "Air is about 78% nitrogen and 21% oxygen."),
    ("SCIENCE", 2, "What is the chemical formula of water?", ["CO2", "H2O", "O2", "NaCl"], 1, "Two hydrogen atoms and one oxygen atom."),
    ("SCIENCE", 2, "Which organ pumps blood around the body?", ["Lungs", "Liver", "Heart", "Brain"], 2, None),
    ("SCIENCE", 2, "Which is the largest planet in our solar system?", ["Saturn", "Jupiter", "Neptune", "Earth"], 1, None),
    ("SCIENCE", 3, "What is the hardest natural substance?", ["Gold", "Iron", "Diamond", "Quartz"], 2, "Diamond is pure carbon arranged in a very strong crystal."),
    ("SCIENCE", 3, "Which part of the cell contains its genetic material?", ["Cell wall", "Nucleus", "Cytoplasm", "Membrane"], 1, None),
    ("SCIENCE", 1, "The Sun is a star.", None, 0, "It is the nearest star to Earth."),
    ("SCIENCE", 2, "Sound travels faster than light.", None, 1, "Light is far faster - that is why you see lightning before you hear thunder."),
    ("SCIENCE", 3, "What force keeps the planets in orbit around the Sun?", ["Magnetism", "Friction", "Gravity", "Electricity"], 2, None),
    # Maths
    ("MATHS", 1, "What is 7 × 8?", ["54", "56", "64", "48"], 1, None),
    ("MATHS", 1, "What is half of 150?", ["50", "70", "75", "80"], 2, None),
    ("MATHS", 1, "How many sides does a hexagon have?", ["5", "6", "7", "8"], 1, None),
    ("MATHS", 2, "What is 15% of 200?", ["15", "20", "30", "35"], 2, "10% is 20 and 5% is 10, so 15% is 30."),
    ("MATHS", 2, "What is the square root of 144?", ["11", "12", "13", "14"], 1, None),
    ("MATHS", 2, "The angles of a triangle add up to how many degrees?", ["90", "180", "270", "360"], 1, None),
    ("MATHS", 2, "Which of these is a prime number?", ["21", "27", "29", "33"], 2, "29 has no factors other than 1 and itself."),
    ("MATHS", 3, "What is 2 to the power of 10?", ["512", "1000", "1024", "2048"], 2, None),
    ("MATHS", 3, "If x + 7 = 20, what is x?", ["13", "27", "14", "3"], 0, None),
    ("MATHS", 3, "What is the area of a rectangle 8 cm long and 5 cm wide?", ["13 cm²", "26 cm²", "40 cm²", "45 cm²"], 2, "Area = length × width = 8 × 5."),
    ("MATHS", 1, "Zero is an even number.", None, 0, "Zero divides by 2 with nothing left over."),
    ("MATHS", 2, "What is 0.5 written as a fraction?", ["1/5", "1/2", "5/1", "2/5"], 1, None),
    # English
    ("ENGLISH", 1, "Which word is a noun?", ["Run", "Happy", "Table", "Quickly"], 2, "A noun names a person, place or thing."),
    ("ENGLISH", 1, "What is the plural of 'child'?", ["Childs", "Children", "Childes", "Childrens"], 1, None),
    ("ENGLISH", 1, "Which is the opposite of 'ancient'?", ["Old", "Modern", "Huge", "Tiny"], 1, None),
    ("ENGLISH", 2, "Which word is an adverb?", ["Slowly", "Slow", "Slowness", "Slower"], 0, "Adverbs often end in -ly and describe how something is done."),
    ("ENGLISH", 2, "Choose the correct spelling.", ["Recieve", "Receive", "Receeve", "Receve"], 1, "I before E, except after C."),
    ("ENGLISH", 2, "What is a synonym of 'big'?", ["Small", "Large", "Thin", "Short"], 1, None),
    ("ENGLISH", 3, "In 'The cat sat on the mat', which word is the verb?", ["Cat", "Sat", "On", "Mat"], 1, None),
    ("ENGLISH", 3, "Which sentence is in the past tense?", ["She sings.", "She will sing.", "She sang.", "She is singing."], 2, None),
    ("ENGLISH", 1, "'Quickly' is an adjective.", None, 1, "It is an adverb; 'quick' is the adjective."),
    # History
    ("HISTORY", 1, "Who is known as the 'Father of the Nation' in India?", ["Jawaharlal Nehru", "Mahatma Gandhi", "Sardar Patel", "B. R. Ambedkar"], 1, None),
    ("HISTORY", 2, "In which year did India gain independence?", ["1945", "1947", "1950", "1952"], 1, "15 August 1947."),
    ("HISTORY", 2, "Who was the first Prime Minister of India?", ["Indira Gandhi", "Lal Bahadur Shastri", "Jawaharlal Nehru", "Rajendra Prasad"], 2, None),
    ("HISTORY", 2, "Which Mughal emperor built the Taj Mahal?", ["Akbar", "Babur", "Shah Jahan", "Aurangzeb"], 2, "Built in memory of his wife Mumtaz Mahal."),
    ("HISTORY", 3, "Who chaired the committee that drafted India's Constitution?", ["B. R. Ambedkar", "Mahatma Gandhi", "Sardar Patel", "Subhas Chandra Bose"], 0, None),
    ("HISTORY", 3, "The ancient Indus Valley city of Mohenjo-daro is in present-day which country?", ["India", "Pakistan", "Afghanistan", "Nepal"], 1, None),
    ("HISTORY", 2, "Who was the first person to walk on the Moon?", ["Yuri Gagarin", "Neil Armstrong", "Buzz Aldrin", "Rakesh Sharma"], 1, "Neil Armstrong, in July 1969."),
    ("HISTORY", 1, "India's Constitution came into effect on 26 January 1950.", None, 0, "That is why 26 January is Republic Day."),
    # Geography
    ("GEOGRAPHY", 1, "What is the capital of India?", ["Mumbai", "Kolkata", "New Delhi", "Chennai"], 2, None),
    ("GEOGRAPHY", 1, "Which is the longest river in India?", ["Yamuna", "Ganga", "Godavari", "Narmada"], 1, None),
    ("GEOGRAPHY", 2, "Which is the highest mountain in the world?", ["K2", "Kangchenjunga", "Mount Everest", "Nanda Devi"], 2, "Mount Everest is about 8,849 m high."),
    ("GEOGRAPHY", 2, "Which desert lies in western India?", ["Sahara", "Thar", "Gobi", "Kalahari"], 1, None),
    ("GEOGRAPHY", 2, "What is the capital of Japan?", ["Beijing", "Seoul", "Tokyo", "Bangkok"], 2, None),
    ("GEOGRAPHY", 3, "Which is the largest country in the world by area?", ["China", "Canada", "USA", "Russia"], 3, None),
    ("GEOGRAPHY", 3, "The Equator divides the Earth into which two halves?", ["East and West", "Northern and Southern hemispheres", "Day and night", "Land and sea"], 1, None),
    ("GEOGRAPHY", 1, "Antarctica is the coldest continent.", None, 0, None),
    # Technology
    ("TECHNOLOGY", 1, "What does 'www' stand for in a web address?", ["World Wide Web", "Wide World Web", "Web World Wide", "World Web Wide"], 0, None),
    ("TECHNOLOGY", 1, "Which device is used to type letters into a computer?", ["Monitor", "Keyboard", "Speaker", "Printer"], 1, None),
    ("TECHNOLOGY", 2, "What does 'CPU' stand for?", ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Power Unit"], 0, "The CPU is the computer's 'brain'."),
    ("TECHNOLOGY", 2, "Which of these is a programming language?", ["HTML", "Python", "JPEG", "USB"], 1, None),
    ("TECHNOLOGY", 3, "How many bits are there in one byte?", ["4", "8", "16", "32"], 1, None),
    ("TECHNOLOGY", 2, "A strong password should be shared with your friends.", None, 1, "Keep passwords private; share them only with a parent or guardian."),
    ("TECHNOLOGY", 3, "Which Indian space mission landed near the Moon's south pole in 2023?", ["Mangalyaan", "Chandrayaan-3", "Gaganyaan", "Aditya-L1"], 1, None),
    # Sports
    ("SPORTS", 1, "How many players are there in a cricket team on the field?", ["9", "10", "11", "12"], 2, None),
    ("SPORTS", 1, "In which sport is a shuttlecock used?", ["Tennis", "Badminton", "Squash", "Table tennis"], 1, None),
    ("SPORTS", 2, "How often are the Summer Olympic Games held?", ["Every 2 years", "Every 3 years", "Every 4 years", "Every 5 years"], 2, None),
    ("SPORTS", 2, "How many rings are there on the Olympic flag?", ["4", "5", "6", "7"], 1, None),
    ("SPORTS", 3, "In chess, which piece can only move diagonally?", ["Rook", "Knight", "Bishop", "King"], 2, None),
    ("SPORTS", 1, "A marathon is shorter than 10 km.", None, 1, "A marathon is about 42.2 km."),
    # Arts
    ("ARTS", 1, "Which of these is a primary colour?", ["Green", "Red", "Purple", "Orange"], 1, None),
    ("ARTS", 2, "Who painted the Mona Lisa?", ["Pablo Picasso", "Vincent van Gogh", "Leonardo da Vinci", "Michelangelo"], 2, None),
    ("ARTS", 2, "Bharatanatyam is a classical dance from which state?", ["Kerala", "Tamil Nadu", "Punjab", "Odisha"], 1, None),
    ("ARTS", 3, "How many keys does a standard piano have?", ["66", "76", "88", "98"], 2, None),
    ("ARTS", 1, "The sitar is a stringed instrument.", None, 0, None),
    # Nature
    ("NATURE", 1, "Which is the largest animal on Earth?", ["Elephant", "Blue whale", "Giraffe", "Shark"], 1, None),
    ("NATURE", 1, "What do bees make?", ["Milk", "Silk", "Honey", "Wax only"], 2, None),
    ("NATURE", 2, "Which bird is known for copying human speech?", ["Crow", "Parrot", "Eagle", "Sparrow"], 1, None),
    ("NATURE", 2, "A tadpole grows into which animal?", ["Fish", "Lizard", "Frog", "Snake"], 2, None),
    ("NATURE", 2, "Which is the fastest land animal?", ["Lion", "Horse", "Cheetah", "Deer"], 2, "A cheetah can sprint at around 100 km/h."),
    ("NATURE", 3, "Animals that eat only plants are called what?", ["Carnivores", "Herbivores", "Omnivores", "Predators"], 1, None),
    ("NATURE", 1, "Bats are mammals.", None, 0, "They are the only mammals that can truly fly."),
    ("NATURE", 3, "Which gas do trees release that we need to breathe?", ["Carbon dioxide", "Oxygen", "Nitrogen", "Helium"], 1, None),
]

CHALLENGES = [
    {"seed_key": "starter:challenge:mixed", "title": "World Quiz Cup", "emoji": "🏆",
     "description": "A bit of everything. How much do you know about the world?", "categories": [], "rounds": 7},
    {"seed_key": "starter:challenge:science", "title": "Science Showdown", "emoji": "🔬",
     "description": "Planets, plants, the human body and more.", "categories": ["SCIENCE", "NATURE"], "rounds": 7},
    {"seed_key": "starter:challenge:maths", "title": "Maths Blitz", "emoji": "➗",
     "description": "Quick sums and sharp thinking.", "categories": ["MATHS"], "rounds": 7},
    {"seed_key": "starter:challenge:india", "title": "Incredible India", "emoji": "🇮🇳",
     "description": "History, geography and culture of India.", "categories": ["HISTORY", "GEOGRAPHY", "ARTS"], "rounds": 7},
]


def install(user) -> dict:
    from app.services.arena.content import forget_memo

    forget_memo()
    now = datetime.utcnow().isoformat()
    existing = {q.get("seed_key") for q in firestore_question_bank.query_documents("scope", "==", "GLOBAL")
                if q.get("seed_key")}
    added = 0
    for index, (category, difficulty, text, options, correct, explanation) in enumerate(QUESTIONS):
        seed_key = f"starter:q:{index}"
        if seed_key in existing:
            continue
        if options is None:
            opts = [{"key": "TRUE", "text": "True"}, {"key": "FALSE", "text": "False"}]
            question_type, key = "TRUE_FALSE", ("TRUE" if correct == 0 else "FALSE")
        else:
            letters = "ABCDEF"
            opts = [{"key": letters[i], "text": t} for i, t in enumerate(options)]
            question_type, key = "MCQ", letters[correct]
        firestore_question_bank.add_document(str(firestore_question_bank.get_next_numeric_id()), {
            "scope": "GLOBAL", "class_id": None, "subject_id": None, "chapter_id": None,
            "category": category, "question_type": question_type, "text": text, "options": opts,
            "correct_key": key, "explanation": explanation, "difficulty": difficulty,
            "is_active": True, "created_by": user.id, "created_at": now, "seed_key": seed_key,
        })
        added += 1

    have = {c.get("seed_key") for c in firestore_arena_challenges.list_all() if c.get("seed_key")}
    challenges = 0
    for challenge in CHALLENGES:
        if challenge["seed_key"] in have:
            continue
        firestore_arena_challenges.add_document(str(firestore_arena_challenges.get_next_numeric_id()), {
            **challenge, "difficulty": None, "starts_at": None, "ends_at": None, "is_active": True,
            "created_by": user.id, "created_at": now,
        })
        challenges += 1
    return {"questions_added": added, "challenges_added": challenges, "questions_in_pack": len(QUESTIONS)}
