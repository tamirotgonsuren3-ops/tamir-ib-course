import copy
import random


EXCEL_QUESTIONS = [
    {
        "id": 1,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion A",
        "question_text": "A1:A5 нүднүүдийн нийлбэрийг олох томьёо аль нь вэ?",
        "options": ["=SUM(A1:A5)", "=ADD(A1:A5)", "=TOTAL(A1:A5)", "=COUNT(A1:A5)"],
        "correct_option_index": 0,
    },
    {
        "id": 2,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion A",
        "question_text": "B1:B10 дотор хэдэн тоон утга байгааг тоолох томьёо аль нь вэ?",
        "options": ["=COUNT(B1:B10)", "=SUM(B1:B10)", "=AVERAGE(B1:B10)", "=LEN(B1:B10)"],
        "correct_option_index": 0,
    },
    {
        "id": 3,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion B",
        "question_text": "C1:C8 нүднүүдийн дундажийг тооцоолох томьёо аль нь вэ?",
        "options": ["=AVERAGE(C1:C8)", "=MEAN(C1:C8)", "=AVG(C1:C8)", "=SUM(C1:C8)/ROW(C1:C8)"],
        "correct_option_index": 0,
    },
    {
        "id": 4,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion B",
        "question_text": "D2 нүдний утга 60-аас бага бол \"Fail\", үгүй бол \"Pass\" гэж буцаах томьёо аль нь вэ?",
        "options": [
            '=IF(D2>=60,"Pass","Fail")',
            '=IF(D2<60,"Pass","Fail")',
            '=IF(D2>=60; "Pass"; "Fail")',
            '=THEN(D2>=60,"Pass","Fail")',
        ],
        "correct_option_index": 0,
    },
    {
        "id": 5,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion C",
        "question_text": "Хуулж наахад хэзээ ч өөрчлөгдөхгүй Absolute reference аль нь вэ?",
        "options": ["$A$1", "A1", "A$1", "$A1"],
        "correct_option_index": 0,
    },
    {
        "id": 6,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion C",
        "question_text": "E1:E20 дотор \"Yes\" гэсэн утгатай хүдрийг тоолох томьёо аль нь вэ?",
        "options": [
            '=COUNTIF(E1:E20,"Yes")',
            '=IF(E1:E20="Yes")',
            '=SUMIF(E1:E20,"Yes",E1:E20)',
            '=COUNT(E1:E20,"Yes")',
        ],
        "correct_option_index": 0,
    },
    {
        "id": 7,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion D",
        "question_text": "A2 утгыг F1:H20 хүснэгтийн эхний баганад хайж, 3 дахь баганын утгыг буцаах томьёо аль нь вэ?",
        "options": [
            "=VLOOKUP(A2,$F$1:$H$20,3,FALSE)",
            "=VLOOKUP(A2,$F$1:$H$20,3,TRUE)",
            "=HLOOKUP(A2,$F$1:$H$20,3,FALSE)",
            "=LOOKUP(A2,$F$1:$H$20,3)",
        ],
        "correct_option_index": 0,
    },
    {
        "id": 8,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion D",
        "question_text": "A2 болон B2 нүдний текстийг хооронд нь нэг хоосон зайтай нэгтгэх томьёо аль нь вэ?",
        "options": [
            '=A2&" "&B2',
            "=A2+B2",
            "=CONCATENATE(A2,B2)",
            '=JOIN(A2," ",B2)',
        ],
        "correct_option_index": 0,
    },
]


PYTHON_QUESTIONS = [
    {
        "id": 1,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion A",
        "question_text": "Python дээр текст дэлгэцэд хэвлэх зөв бичгийн дүрэм аль нь вэ?",
        "options": ['print("Hello")', 'echo("Hello")', 'console.log("Hello")', 'printf("Hello")'],
        "correct_option_index": 0,
    },
    {
        "id": 2,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion A",
        "question_text": "Python хувьсагчид утга оноох зөв хэлбэр аль нь вэ?",
        "options": ["count = 10", "10 = count", "var count = 10", "count := 10"],
        "correct_option_index": 0,
    },
    {
        "id": 3,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion B",
        "question_text": "`for i in range(3):` хэдэн удаа давтагдах вэ?",
        "options": ["3 удаа: 0, 1, 2", "3 удаа: 1, 2, 3", "4 удаа: 0, 1, 2, 3", "Тодорхойгүй удаа"],
        "correct_option_index": 0,
    },
    {
        "id": 4,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion B",
        "question_text": "`names` жагсаалтын элемент бүрийг нэг нэгээр хэвлэх for loop аль нь вэ?",
        "options": [
            "for name in names: print(name)",
            "for names in name: print(names)",
            "while names: print(names)",
            "foreach names as name: print(name)",
        ],
        "correct_option_index": 0,
    },
    {
        "id": 5,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion C",
        "question_text": "Нас 18-аас дээш бол \"adult\", үгүй бол \"minor\" хэвлэх код аль нь вэ?",
        "options": [
            'if age >= 18:\n    print("adult")\nelse:\n    print("minor")',
            'if age >= 18\n    print("adult")\nelse print("minor")',
            'if (age >= 18) {\n    print("adult")\n} else {\n    print("minor")\n}',
            'when age >= 18 then print("adult") otherwise print("minor")',
        ],
        "correct_option_index": 0,
    },
    {
        "id": 6,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion C",
        "question_text": "Python дээр кодын блок (if, for, function) хэрхэн зааглагдах вэ?",
        "options": [
            "Нэг ижил түвшний хоосон зайгаар (ихэвчлэн 4)",
            "{} хаалтаар",
            "Тоосон цэг (;)-ээр",
            "Хаалт () -аар",
        ],
        "correct_option_index": 0,
    },
    {
        "id": 7,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion D",
        "question_text": "Python скриптийг хадгалахад зөв файлын өргөтгөл аль нь вэ?",
        "options": ["script.py", "script.txt", "script.pyc", "script.exe"],
        "correct_option_index": 0,
    },
    {
        "id": 8,
        "question_type": "multiple_choice",
        "target_criteria": "Criterion D",
        "question_text": "`script.py`-г терминалээс ажиллуулж `data.csv` гэсэн аргумент дамжуулах зөв командын аль нь вэ?",
        "options": [
            "python script.py data.csv",
            "python data.csv script.py",
            "run script.py data.csv",
            "python -f script.py --arg data.csv",
        ],
        "correct_option_index": 0,
    },
]


TOPIC_QUIZZES = {
    "excel": {
        "title": "Excel томьёо — 4 хариулттай тест",
        "description": "SUM, AVERAGE, IF, COUNTIF, VLOOKUP, absolute reference болон текст нэгтгэх томьёо.",
        "questions": EXCEL_QUESTIONS,
    },
    "python": {
        "title": "Python үндэс — 4 хариулттай тест",
        "description": "Синтаксис, for loop, if/else, скрипт хадгалах/ажиллуулах, аргумент дамжуулах.",
        "questions": PYTHON_QUESTIONS,
    },
}


def build_topic_quiz(topic: str) -> dict:
    quiz = TOPIC_QUIZZES[topic]
    questions = copy.deepcopy(quiz["questions"])
    for question in questions:
        correct_text = question["options"][question["correct_option_index"]]
        pairs = list(enumerate(question["options"]))
        random.Random(f"{topic}-{question['id']}").shuffle(pairs)
        question["options"] = [text for _, text in pairs]
        question["correct_option_index"] = next(
            index for index, (_, text) in enumerate(pairs) if text == correct_text
        )
    return {"quiz_title": quiz["title"], "questions": questions}
