# Підпис і нотаризація macOS-збірки — що зробити в Apple

Щоб інсталятор відкривався подвійним кліком без «unidentified developer», збірку
треба підписати (застосунок — **Developer ID Application**, пакет — **Developer ID
Installer**) і пронотаризувати в Apple.
Сертифікат і креденшели створює власник акаунта один раз; CI далі робить усе саме.

Нижче — рівно ті кроки, які потрібні, і рівно ті пʼять значень, які треба покласти
в секрети GitHub.

> Секрети нікуди не надсилай текстом — ні в чат, ні в тікет. Додавай їх одразу в
> GitHub: репозиторій `goodpesik/barhandler-manager` → **Settings** →
> **Secrets and variables** → **Actions** → **New repository secret**.

---

## 1. Сертифікат Developer ID Application

Це НЕ той сертифікат, яким підписуються збірки для App Store. Для застосунку, що
розповсюджується поза магазином, потрібен саме **Developer ID Application**.

**Найпростіший шлях — через Xcode:**

1. Xcode → **Settings** (⌘,) → **Accounts**.
2. Обери свій Apple ID → кнопка **Manage Certificates…**
3. Кнопка **+** унизу ліворуч → **Developer ID Application**.
4. Xcode сам зробить запит (CSR), випустить сертифікат і покладе його в твою
   login-keychain.

**Якщо Xcode не під рукою — через сайт:**

1. Keychain Access → меню **Certificate Assistant** → **Request a Certificate
   From a Certificate Authority**. Email — твій, Common Name — будь-яке,
   **Saved to disk**. Отримаєш файл `CertificateSigningRequest.certSigningRequest`.
2. [developer.apple.com/account/resources/certificates](https://developer.apple.com/account/resources/certificates)
   → **+** → розділ **Software** → **Developer ID Application** → Continue.
3. Завантаж свій `.certSigningRequest` → Continue → **Download** (`.cer`).
4. Подвійний клік по завантаженому `.cer` — він стане в login-keychain.

Перевірка, що все на місці (термінал):

```bash
security find-identity -v -p codesigning
```

У переліку має бути рядок виду:

```
1) A1B2C3… "Developer ID Application: Maksym Levynets (ABCDE12345)"
```

> Apple дає обмежену кількість Developer ID сертифікатів на акаунт (їх складно
> відкликати), тож не створюй їх «про запас» — одного достатньо для всіх наших
> продуктів.

---

## 2. Експорт сертифіката у `.p12`

CI не має доступу до твоєї keychain, тож сертифікат разом із приватним ключем
треба віддати йому файлом.

1. Keychain Access → зліва **login** → вкладка **My Certificates**.
2. Знайди `Developer ID Application: … (TEAMID)`.
3. Правий клік → **Export…** → формат **Personal Information Exchange (.p12)**.
4. Придумай пароль на файл — саме він піде в секрет `MAC_CERT_PASSWORD`.

Переведи файл у base64 (GitHub-секрет тримає текст, не файл):

```bash
base64 -i ~/Downloads/developer-id.p12 | pbcopy
```

Вміст буфера → секрет `MAC_CERT_P12_BASE64`.

---

## 3. Пароль для нотаризації

Нотаризація — це окремий запит до Apple, і він ходить під app-specific паролем,
а не під основним паролем Apple ID.

1. [appleid.apple.com](https://appleid.apple.com) → **Sign-In and Security** →
   **App-Specific Passwords** → **+**.
2. Назва — щось на кшталт `notarytool bhm`.
3. Отримаєш пароль виду `abcd-efgh-ijkl-mnop` → секрет `MAC_NOTARY_PASSWORD`.

Твій Apple ID (email) → секрет `MAC_NOTARY_APPLE_ID`.

---

## 4. Team ID

[developer.apple.com/account](https://developer.apple.com/account) →
**Membership details** → рядок **Team ID**, десять символів (він же в дужках у
назві сертифіката) → секрет `MAC_NOTARY_TEAM_ID`.

---

## 5. Підсумок: пʼять секретів

| Секрет | Що це | Звідки |
|---|---|---|
| `MAC_CERT_P12_BASE64` | сертифікат + приватний ключ, base64 | крок 2 |
| `MAC_CERT_PASSWORD` | пароль, яким захищений `.p12` | крок 2 |
| `MAC_NOTARY_APPLE_ID` | твій Apple ID (email) | крок 3 |
| `MAC_NOTARY_PASSWORD` | app-specific пароль | крок 3 |
| `MAC_NOTARY_TEAM_ID` | Team ID (10 символів) | крок 4 |

---

## Що робить CI, коли секрети є

`scripts/mac_sign_and_package.sh` (його кличуть обидва мак-джоби):

1. створює тимчасову keychain і ставить туди `.p12`;
2. підписує `.app` сертифікатом Developer ID з **hardened runtime**, міткою часу
   і entitlements з `installers/entitlements-mac.plist`;
3. кличе `scripts/mac_build_pkg.sh`, поки keychain ще живий: той збирає `.pkg`
   (`pkgbuild` + `productbuild` з майстром) і підписує його сертифікатом
   **Developer ID Installer** — це ІНШИЙ сертифікат, `codesign` тут не годиться;
4. надсилає `.pkg` у нотаризацію (`xcrun notarytool submit --wait`), чекає
   вердикту, робить `xcrun stapler staple`;
5. перевіряє результат (`spctl -a -t install`) і зносить тимчасову keychain.

Без секретів (форк, чужа гілка, нічна збірка) той самий скрипт підписує ad-hoc і
нотаризацію пропускає — збірка не падає, але такий пакет система відкриє лише
після дозволу в **System Settings → Privacy & Security**.

## Чому потрібні entitlements

Збірка PyInstaller — одним файлом: при запуску вона розпаковує свої `.so`/`.dylib`
у тимчасову теку й вантажить їх звідти. Hardened runtime за замовчуванням таке
забороняє, тому в `installers/entitlements-mac.plist` стоять два послаблення:

- `com.apple.security.cs.disable-library-validation` — дозволяє підвантажувати
  бібліотеки, підписані не тим самим Team ID (libusb, бібліотеки Python);
- `com.apple.security.cs.allow-unsigned-executable-memory` — CPython цього
  потребує для власної роботи.

Без них підписаний застосунок нотаризацію пройде, але при запуску впаде.
