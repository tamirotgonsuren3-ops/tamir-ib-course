# Веб сайтад гэрийн даалгавар, ирц, сахилгын оноо нэмэх хэрэгжүүлэлтийн промпт

Доорх промптыг та өөрийн хөгжүүлэгч (эсвэл AI кодлогч) -т бүтэн өгч болно. Энэ нь шаардлагыг модуль болгон задалж, өгөгдлийн загвар, API, UI, Ollama үнэлгээний урсгал, багшийн тохиргоог бүгдийг хамарсан.

---

## Промпт (хуулах)

```
Та миний шалгалт авдаг веб сайтад дараах функцуудыг нэмж хэрэгжүүлнэ үү.
Одоо байгаа стек: <ЭНД ӨӨРИЙН СТЕКЭЭ БИЧ, жишээ: Next.js + Node/Express + PostgreSQL + Prisma>
Хэрэглэгчийн төрөл: Teacher (багш), Student (сурагч), Admin.
Бүх UI текст Монгол хэлээр байна. Кодын тайлбар англи хэлээр байж болно.

=== 1. ӨГӨГДЛИЙН ЗАГВАР (Prisma жишээ, шаардлагатай бол өөр ORM-д тохируул) ===

model Class {
  id        String   @id @default(cuid())
  name      String
  teacherId String
  students  Student[]
}

model Student {
  id        String   @id @default(cuid())
  classId   String
  name      String
  homeworkSubmissions HomeworkSubmission[]
  attendanceRecords   AttendanceRecord[]
  disciplineRecords   DisciplineRecord[]
}

model Homework {
  id          String   @id @default(cuid())
  classId     String
  title       String
  description String   // Даалгаврын тайлбар
  starterCode String?  // Сурагчид өгөх эх код (сонголтоор)
  maxScore    Int      // Багшийн тогтоосон дээд оноо (жишээ: 10)
  checkMode   String   // "recursive" эсвэл "queue" — багш сонгоно
  dueDate     DateTime
  tests       HomeworkTest[]  // Нэгж тестүүд (хариулт шалгах)
  submissions HomeworkSubmission[]
}

model HomeworkTest {
  id         String @id @default(cuid())
  homeworkId String
  name       String
  testCode   String // Python тестийн код, жишээ: "def test_x(f): return f(2,3)==5, '...'"
}

model HomeworkSubmission {
  id          String   @id @default(cuid())
  homeworkId  String
  studentId   String
  code        String   // Сурагчийн бичсэн код
  status      String   // "pending" | "checking" | "done" | "error"
  score       Int?     // LLM/тестийн гаргасан оноо
  maxScore    Int
  explanation String?  @db.Text // Шалгасан тайлбар (Монгол хэлээр)
  testResults Json?    // Тест бүрийн үр дүн
  submittedAt DateTime @default(now())
  checkedAt   DateTime?
}

model AttendanceRecord {
  id        String   @id @default(cuid())
  studentId String
  date      DateTime
  status    String   // "present" | "absent" | "late"
  score     Int      // Багшийн тогтоосон ирцийн оноо (жишээ: ирсэн +2, тасалсан 0)
}

model DisciplineRecord {
  id        String   @id @default(cuid())
  studentId String
  date      DateTime
  note      String   // жишээ: "played game, did not pay attention"
  scoreDelta Int     // Сөрөг тоо, жишээ: -3
}

model ClassScoringConfig {
  id                String @id @default(cuid())
  classId           String @unique
  attendancePresent Int    @default(2)  // Ирсэн оноо
  attendanceLate    Int    @default(1)  // Хоцорсон оноо
  attendanceAbsent  Int    @default(0)  // Тасалсан оноо
  maxHomeworkScore  Int    @default(10)
}

=== 2. БАГШИЙН ТОХИРГОО (UI) ===

A. Гэрийн даалгавар үүсгэх хуудас (/teacher/homework/new):
   - Гарчиг, тайлбар, эх код, дээд оноо (maxScore) оруулна.
   - "Шалгах горим" сонголт: Radio button — "Рекурсив (for loop)" эсвэл "Дараалал (Queue)".
     Тайлбар текст: "Рекурсив горимд даалгаврыг шууд шалгана; Дараалал горимд
     сурагчдын илгээлтийг дараалалд хийж, арын дэвсгэр процесс шалгана."
   - Нэгж тестүүд нэмэх хэсэг (testCode талбар).
   - Due date.

B. Ирцийн тохиргоо (/teacher/class/:id/attendance-settings):
   - Ирсэн / Хоцорсон / Тасалсан тус бүрийн оноог тохируулна (ClassScoringConfig).

C. Ирц бүртгэх (/teacher/class/:id/attendance):
   - Огноо сонгоод, сурагч бүрийн төлөв (present/late/absent) сонгоно.
   - Хадгалахад ClassScoringConfig-ын дагуу оноо автоматаар тооцогдоно.

D. Сахилгын бүртгэл (/teacher/class/:id/discipline):
   - Сурагч сонгоод, тайлбар (жишээ: "played game, did not pay attention"),
     хасах оноо (scoreDelta, сөрөг) оруулна.

=== 3. СУРАГЧИЙН ГЭРИЙН ДААЛГАВАР ИЛГЭЭХ (/student/homework/:id) ===

   - Даалгаврын тайлбар, эх код, due date харагдана.
   - Код бичих textarea (эсвэл Monaco editor).
   - "Илгээх" товч → POST /api/homework/:id/submit
   - Илгээсний дараа статус "pending" болж, шалгагдаж дуустал "Шалгаж байна..." гэж харагдана.
   - Шалгагдаж дууссаны дараа оноо, тайлбар (explanation), тест бүрийн үр дүн харагдана.

=== 4. ШАЛГАХ УРСГАЛ (Ollama + нэгж тест) ===

Багшийн сонгосон checkMode-оос хамаарч:

-- "recursive" горим: илгээх үед шалгалтыг шууд (synchronously) гүйцэтгэнэ.
   Жижиг тооны сурагчтай ангид тохиромжтой.

-- "queue" горим: илгээлтийг Redis/BullMQ (эсвэл DB-д суурилсан энгийн дараалал)
   -д хийж, арын дэвсгэр worker шалгана. Олон сурагч зэрэг илгээхэд тохиромжтой.

Шалгах алхмууд (worker эсвэл шууд):
   1. Сурагчийн кодыг тусгаарлагдсан орчинд (sandbox: Docker container эсвэл
      `python -I` + timeout + memory limit) ажиллуулна. Хэзээ ч серверийн үндсэн
      процесст шууд exec хийхгүй.
   2. HomeworkTest бүрийг ажиллуулж, testResults-д хадгална.
      - Бүх тест давсан эсэх, аль нь унасан.
   3. Ollama LLM (CPU, model: llama3.2:3b) руу дараах prompt илгээнэ:

      """
      Чи Python хичээлийн багшийн туслах. Сурагчийн кодыг үнэл.
      Даалгавар: {homework.description}
      Сурагчийн код:
      ```python
      {submission.code}
      ```
      Нэгж тестийн үр дүн: {testResults}
      Дээд оноо: {homework.maxScore}

      Дүрэм:
      - Хэрэв бүх тест давсан бол кодын логик (бодолт) зөв эсэхийг шалга.
        Зөв бол магтаал + оноо өг. Код зөв боловч логик буруу бол хаана буруу
        ойлгосныг зааж, оноог бууруул.
      - Хэрэв тест унасан бол бүрэн шийдлийг өгөхгүйгээр аль хэсэгт алдаа
        гарсныг чиглүүлэх асуулт хэлбэрээр зааж, оноог бууруул.
      - Зөвхөн Монгол хэлээр хариул.
      - Дараах JSON форматаар л хариул:
        {"score": <0..maxScore бүхэл тоо>, "explanation": "<Монгол тайлбар>"}
      """
   4. JSON-г parse хийж, HomeworkSubmission.score, .explanation, .status="done",
      .checkedAt-д хадгална. Алдаа гарвал status="error", explanation-д шалтгаан бичнэ.

=== 5. API ENDPOINTS ===

POST /api/homework/:id/submit          — сурагч илгээх (checkMode-оор шууд эсвэл дараалалд)
GET  /api/homework/submission/:subId   — оноо, тайлбар, тестийн үр дүн
POST /api/teacher/homework             — даалгавар үүсгэх
PUT  /api/teacher/class/:id/scoring    — ирцийн оноо тохируулах
POST /api/teacher/class/:id/attendance — ирц бүртгэх (bulk)
POST /api/teacher/class/:id/discipline — сахилгын бүртгэл + хасах оноо
GET  /api/student/:id/report           — сурагчийн нэгдсэн тайлан:
       гэрийн даалгаврын оноо, ирцийн оноо, сахилгын хасалт, нийт дүн

=== 6. ДҮН ХАРАГДАХ (UI) ===

A. Сурагчийн хувьд: даалгавар бүрийн оноо/дээд оноо, тайлбар, тестийн дэлгэрэнгүй.
B. Багшийн хувьд: ангийн бүх сурагчийн гэрийн даалгавар, ирц, сахилгын нэгдсэн
   хүснэгт; сурагч тус бүрийн нийт оноо = (гэрийн даалгаврын оноо) + (ирцийн оноо)
   + (сахилгын scoreDelta). Нийт оноог автоматаар тооцож харуулна.

=== 7. АЮУЛГҮЙ БАЙДАЛ, ЧАНАР ===

- Сурагчийн кодыг заавал sandbox-д ажиллуул (timeout 5 сек, memory 128MB, сүлжээгүй).
- Ollama дуудлагад timeout тавь (жишээ: 60 сек), алдаа гарвал retry 1 удаа.
- Бүх API-д role-based authorization: зөвхөн тухайн ангийн багш тохиргоо хийх,
  зөвхөн тухайн сурагч өөрийн илгээлтийг харах.
- Оноо 0-ээс maxScore хооронд байхыг серверт шалга.

=== 8. ХҮЛЭЭГДЭХ ҮР ДҮН ===

- Ажиллах код (migration, API, worker, UI бүрэн).
- Жишээ өгөгдөл (seed): 1 анги, 2 сурагч, 1 гэрийн даалгавар (2 тесттэй).
- README: Ollama суулгах (`ollama pull llama3.2:3b`), worker ажиллуулах заавар.
- Аль горим (recursive/queue) хэзээ тохиромжтой тайлбар.
```

---

**Хэрэглэх зөвлөмж:** Эхний мөрөнд `<ЭНД ӨӨРИЙН СТЕКЭЭ БИЧ>` гэсэн хэсгийг өөрийн веб сайтын технологиор сольж бичээрэй (жишээ нь Django + PostgreSQL, эсвэл Laravel + MySQL). Хэрэв таны сайт аль хэдийн өгөгдлийн сантай бол `Class`, `Student` модель дээр өөрийн одоогийн хүснэгтүүдтэй холбох заавар нэмж өгөхийг хүсээрэй. Мөн sandbox хэсэгт Docker ашиглах боломжгүй бол `RestrictedPython` эсвэл тусдаа `subprocess` + `resource.setrlimit` ашиглах хувилбарыг хүсч болно.