;;; apple-music-metadata-review-test.el --- Tests for metadata review  -*- lexical-binding: t; -*-

(require 'ert)
(require 'json)
(require 'apple-music-metadata-review)

(defun apple-music-metadata-review-test--csv-field (value)
  (if (string-match-p "[\",\r\n]" value)
      (concat "\"" (replace-regexp-in-string "\"" "\"\"" value t t) "\"")
    value))

(defun apple-music-metadata-review-test--row
    (id status current-title suggested-title &rest overrides)
  (let ((row (mapcar (lambda (column) (cons column ""))
                     apple-music-metadata-review--report-columns)))
    (setf (alist-get "persistent_id" row nil nil #'equal) id
          (alist-get "match_status" row nil nil #'equal) status
          (alist-get "current_title" row nil nil #'equal) current-title
          (alist-get "current_artist" row nil nil #'equal) "Current Artist"
          (alist-get "current_album" row nil nil #'equal) "Current Album"
          (alist-get "suggested_title" row nil nil #'equal) suggested-title
          (alist-get "reasons" row nil nil #'equal) "manual review"
          (alist-get "acoustid_id" row nil nil #'equal) "acoustid-1"
          (alist-get "acoustid_score" row nil nil #'equal) "0.912345"
          (alist-get "musicbrainz_recording_id" row nil nil #'equal) "recording-1"
          (alist-get "musicbrainz_release_id" row nil nil #'equal) "release-1"
          (alist-get "beets_recommendation" row nil nil #'equal) "medium"
          (alist-get "beets_distance" row nil nil #'equal) "0.123456"
          (alist-get "distance_penalties" row nil nil #'equal) "{\"title\":0.1}"
          (alist-get "evidence" row nil nil #'equal) "fingerprint and tags"
          (alist-get "source_urls" row nil nil #'equal)
          "https://acoustid.org/track/1 | https://musicbrainz.org/recording/1"
          (alist-get "playlists" row nil nil #'equal) "Favorites | Mix"
          (alist-get "duration_seconds" row nil nil #'equal) "183.5"
          (alist-get "location" row nil nil #'equal) "/Music/Test.mp3")
    (dolist (override overrides)
      (setf (alist-get (car override) row nil nil #'equal) (cdr override)))
    row))

(defun apple-music-metadata-review-test--change
    (id status current-title suggested &optional approved)
  `((persistent_id . ,id)
    (approved . ,(if approved t :json-false))
    (match_status . ,status)
    (current . ((title . ,current-title)
                (artist . "Current Artist")
                (album . "Current Album")))
    (suggested . ,suggested)))

(defun apple-music-metadata-review-test--write-json (path value)
  (with-temp-file path
    (insert (decode-coding-string
             (json-serialize value
                             :null-object :json-null
                             :false-object :json-false)
             'utf-8)
            "\n")))

(defun apple-music-metadata-review-test--fixture (changes rows)
  (let* ((directory (make-temp-file "metadata-review-test-" t))
         (report (expand-file-name "report.csv" directory))
         (plan-file (expand-file-name "plan.json" directory))
         (columns
          (if (seq-some
               (lambda (row) (assoc "cover_art_url" row))
               rows)
              (append apple-music-metadata-review--report-columns
                      apple-music-metadata-review--optional-report-columns)
            apple-music-metadata-review--report-columns))
         (plan `((snapshot . "/tmp/snapshot.sqlite3")
                 (audit . "/tmp/audit.csv")
                 (review_report . ,report)
                 (retained . ((nested . [1 2 3])))
                 (metadata_changes . ,(vconcat changes)))))
    (with-temp-file report
      (insert (mapconcat #'apple-music-metadata-review-test--csv-field
                         columns ",")
              "\n")
      (dolist (row rows)
        (insert
         (mapconcat
          #'apple-music-metadata-review-test--csv-field
          (mapcar (lambda (column) (alist-get column row nil nil #'equal))
                  columns)
          ",")
         "\n")))
    (apple-music-metadata-review-test--write-json plan-file plan)
    (list :directory directory :report report :plan-file plan-file :plan plan)))

(defun apple-music-metadata-review-test--cleanup (directory)
  (dolist (buffer (buffer-list))
    (when (buffer-live-p buffer)
      (with-current-buffer buffer
        (when (or (derived-mode-p 'apple-music-metadata-review-mode)
                  (derived-mode-p 'apple-music-metadata-review-detail-mode)
                  (and buffer-file-name
                       (string-prefix-p directory buffer-file-name)))
          (set-buffer-modified-p nil)
          (kill-buffer buffer)))))
  (delete-directory directory t))

(cl-defmacro apple-music-metadata-review-test--with-fixture
    ((fixture changes rows) &rest body)
  (declare (indent 1) (debug (symbolp form form body)))
  `(let* ((,fixture (apple-music-metadata-review-test--fixture ,changes ,rows))
          (directory (plist-get ,fixture :directory)))
     (unwind-protect
         (progn ,@body)
       (apple-music-metadata-review-test--cleanup directory))))

(defun apple-music-metadata-review-test--read-plan (path)
  (with-temp-buffer
    (insert-file-contents path)
    (json-parse-buffer :object-type 'alist :array-type 'array
                       :null-object :json-null :false-object :json-false)))

(ert-deftest apple-music-metadata-review-csv-parser ()
  (should
   (equal
    (apple-music-metadata-review--parse-csv-string
     "id,name,note,empty\r\n1,Beyoncé,plain,\r\n2,\"a,b\",\"line 1\r\nline \"\"2\"\"\",\r\n")
    '(("id" "name" "note" "empty")
      ("1" "Beyoncé" "plain" "")
      ("2" "a,b" "line 1\nline \"2\"" ""))))
  (let ((quote-error
         (should-error
          (apple-music-metadata-review--parse-csv-string "a\nno\"pe"))))
    (should (string-match-p "Malformed review CSV record 2: quote inside"
                            (error-message-string quote-error))))
  (let ((unterminated
         (should-error
          (apple-music-metadata-review--parse-csv-string "a\n\"open"))))
    (should (string-match-p "Malformed review CSV record 2: unterminated"
                            (error-message-string unterminated)))))

(ert-deftest apple-music-metadata-review-open-filters-and-preserves-order ()
  (let* ((strong (apple-music-metadata-review-test--change
                  "S" "strong_candidate" "Strong" '((title . "Better"))))
         (first (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
         (second (apple-music-metadata-review-test--change
                  "N2" "needs_review" "Second" '((title . "Second new")) t))
         (rows (list
                (apple-music-metadata-review-test--row
                 "S" "strong_candidate" "Strong" "Better")
                (apple-music-metadata-review-test--row
                 "N1" "needs_review" "First" "First new")
                (apple-music-metadata-review-test--row
                 "N2" "needs_review" "Second" "Second new")
                (apple-music-metadata-review-test--row
                 "REPORT-ONLY" "needs_review" "No proposal" ""))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list strong first second) rows)
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (should (eq (lookup-key apple-music-metadata-review-mode-map
                              (kbd "C-x C-s"))
                  #'apple-music-metadata-review-save))
      (should (equal (mapcar (lambda (entry)
                              (alist-get 'persistent_id (car entry)))
                            apple-music-metadata-review--entries)
                     '("N1" "N2")))
      (should (= (length (alist-get 'metadata_changes
                                    apple-music-metadata-review--plan)) 3))
      (goto-char (point-min))
      (should (equal (tabulated-list-get-id) "N1"))
      (forward-line 1)
      (should (equal (tabulated-list-get-id) "N2"))
      (should (equal mode-line-process
                     '("  " "Needs review: 2  Approved: 1  Remaining: 1")))
      (should
       (equal
        (mapcar (lambda (column) (list (car column) (cadr column)))
                (append tabulated-list-format nil))
        '(("Approved" 10)
          ("Current title" 25)
          ("Suggested title" 25)
          ("Current artist" 21)
          ("Suggested artist" 21)
          ("Current album" 20)
          ("Suggested album" 20)))))))

(ert-deftest apple-music-metadata-review-overview-keeps-comparison-columns-separate ()
  (let* ((change
          (apple-music-metadata-review-test--change
           "N1" "needs_review"
           "[Official Video] A very long download title that would hide the song"
           '((title . "Canonical Song"))))
         (apple-music-metadata-review--entries (list (cons change nil)))
         (columns (cadr (car (apple-music-metadata-review--tabulated-entries)))))
    (should
     (equal (aref columns 1)
            "[Official Video] A very long download title that would hide the song"))
    (should (equal (substring-no-properties (aref columns 2)) "Canonical Song"))
    (should (eq (get-text-property 0 'face (aref columns 2)) 'success))
    (should (equal (substring-no-properties (aref columns 4)) "(not suggested)"))
    (should (eq (get-text-property 0 'face (aref columns 4)) 'shadow))))

(ert-deftest apple-music-metadata-review-pixel-aligns-wide-glyph-columns ()
  (with-temp-buffer
    (setq tabulated-list-format [("Wide" 4 t) ("Next" 4 t)]
          tabulated-list-padding 2)
    (cl-letf (((symbol-function 'display-graphic-p)
               (lambda (&optional _display) t))
              ((symbol-function 'string-pixel-width)
               (lambda (string &optional _buffer)
                 (seq-reduce
                  (lambda (width character)
                    (+ width (if (> character 127) 20 10)))
                  (string-to-list (substring-no-properties string))
                  0))))
      (apple-music-metadata-review--print-entry
       "N1" ["日本語" "Next"])
      (goto-char (point-min))
      (should-not (search-forward "日本語" nil t))
      (goto-char (point-min))
      (search-forward "Next")
      (should
       (equal
        (get-text-property (1- (match-beginning 0)) 'display)
        '(space :align-to (70)))))))

(ert-deftest apple-music-metadata-review-sparse-suggestions-must-match ()
  (let ((change (apple-music-metadata-review-test--change
                 "N1" "needs_review" "Old" '((title . "New")))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "Old" "New")))
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (should (= (length apple-music-metadata-review--entries) 1)))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "Old" "New"
                        '("suggested_artist" . "Missing from plan"))))
      (let ((error-data
             (should-error
              (apple-music-metadata-review-open (plist-get fixture :plan-file)))))
        (should
         (equal (error-message-string error-data)
                "Apply plan and review report disagree for persistent_id: N1"))))))

(ert-deftest apple-music-metadata-review-detail-shows-evidence-and-links ()
  (let ((change (apple-music-metadata-review-test--change
                 "N1" "needs_review" "" '((title . "New title")))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "" "New title")))
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (goto-char (point-min))
      (apple-music-metadata-review-show-details)
      (dolist (text '("Metadata review 1 of 1" "Persistent ID: N1"
                      "(empty)" "(not suggested)" "Reasons: manual review"
                      "Beets recommendation: medium" "Beets distance: 0.123456"
                      "Distance penalties: {\"title\":0.1}"
                      "AcoustID ID: acoustid-1" "AcoustID score: 0.912345"
                      "MusicBrainz recording ID: recording-1"
                      "MusicBrainz release ID: release-1"
                      "Evidence: fingerprint and tags" "Playlists: Favorites | Mix"
                      "Duration: 183.5" "Location: /Music/Test.mp3"))
        (should (string-match-p (regexp-quote text) (buffer-string))))
      (dolist (url '("https://acoustid.org/track/1"
                     "https://musicbrainz.org/recording/1"))
        (goto-char (point-min))
        (search-forward url)
        (should (button-at (match-beginning 0)))))))

(ert-deftest apple-music-metadata-review-artwork-detail-and-approval-persist ()
  (let* ((url "https://coverartarchive.org/release/release-1/front-1200.jpg")
         (artwork `((source . "cover_art_archive")
                    (release_id . "release-1")
                    (url . ,url)))
         (change
          (append
           (apple-music-metadata-review-test--change
            "N1" "needs_review" "Old" '((title . "New")))
           `((artwork . ,artwork))))
         (row
          (apple-music-metadata-review-test--row
           "N1" "needs_review" "Old" "New"
           (cons "cover_art_url" url)
           (cons "source_urls"
                 (concat "https://musicbrainz.org/release/release-1 | " url)))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let ((plan-file (plist-get fixture :plan-file))
            (backup-directory-alist nil))
        (apple-music-metadata-review-open plan-file)
        (goto-char (point-min))
        (apple-music-metadata-review-show-details)
        (dolist (text '("Artwork" "(checked during apply)"
                        "Add exact release front if missing"
                        "Cover art URL:"))
          (should (string-match-p (regexp-quote text) (buffer-string))))
        (goto-char (point-min))
        (search-forward "Source URLs:")
        (search-forward url)
        (should (button-at (match-beginning 0)))
        (apple-music-metadata-review-approve)
        (apple-music-metadata-review-save)
        (let* ((saved (apple-music-metadata-review-test--read-plan plan-file))
               (saved-change (aref (alist-get 'metadata_changes saved) 0)))
          (should (eq (alist-get 'approved saved-change) t))
          (should (equal (alist-get 'artwork saved-change) artwork)))))))

(ert-deftest apple-music-metadata-review-artwork-validation-and-report-agreement ()
  (let* ((url "https://coverartarchive.org/release/release-1/front.jpg")
         (base
          (apple-music-metadata-review-test--change
           "N1" "needs_review" "Old" '((title . "New"))))
         (artwork `((source . "cover_art_archive")
                    (release_id . "release-1")
                    (url . ,url)))
         (change (append base `((artwork . ,artwork)))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "Old" "New")))
      (should-error
       (apple-music-metadata-review-open (plist-get fixture :plan-file))
       :type 'error))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "Old" "New"
                        '("cover_art_url" . "https://example.com/wrong.jpg"))))
      (should-error
       (apple-music-metadata-review-open (plist-get fixture :plan-file))
       :type 'error))
    (apple-music-metadata-review-test--with-fixture
        (fixture
         (list (append base
                       '((artwork . ((source . "wrong")
                                     (release_id . "release-1")
                                     (url . "https://example.com/front.jpg"))))))
         (list (apple-music-metadata-review-test--row
                "N1" "needs_review" "Old" "New"
                (cons "cover_art_url" url))))
      (should-error
       (apple-music-metadata-review-open (plist-get fixture :plan-file))
       :type 'error))))

(ert-deftest apple-music-metadata-review-legacy-report-has-empty-cover-art ()
  (let ((change (apple-music-metadata-review-test--change
                 "N1" "needs_review" "Old" '((title . "New")))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N1" "needs_review" "Old" "New")))
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (should
       (equal
        (apple-music-metadata-review--row-value
         (cdar apple-music-metadata-review--entries) "cover_art_url")
        "")))))

(ert-deftest apple-music-metadata-review-actions-and-navigation ()
  (let* ((first (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
         (second (apple-music-metadata-review-test--change
                  "N2" "needs_review" "Second" '((title . "Second new"))))
         (rows (list
                (apple-music-metadata-review-test--row
                 "N1" "needs_review" "First" "First new")
                (apple-music-metadata-review-test--row
                 "N2" "needs_review" "Second" "Second new"))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list first second) rows)
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (goto-char (point-min))
      (let ((overview (current-buffer)))
        (apple-music-metadata-review-show-details)
        (apple-music-metadata-review-unapprove)
        (should-not (with-current-buffer overview (buffer-modified-p)))
        (apple-music-metadata-review-approve)
        (should (string-match-p "Status: Approved" (buffer-string)))
        (should (with-current-buffer overview
                  (and (buffer-modified-p)
                       (equal (tabulated-list-get-id) "N1")
                       (equal mode-line-process
                              '("  " "Needs review: 2  Approved: 1  Remaining: 1")))))
        (apple-music-metadata-review-toggle)
        (should (string-match-p "Status: Not approved" (buffer-string)))
        (apple-music-metadata-review-next)
        (should (equal apple-music-metadata-review--entry-id "N2"))
        (should (with-current-buffer overview
                  (equal (tabulated-list-get-id) "N2")))
        (apple-music-metadata-review-previous)
        (should (equal apple-music-metadata-review--entry-id "N1"))
        (apple-music-metadata-review-previous)
        (should (equal apple-music-metadata-review--entry-id "N1"))))))

(ert-deftest apple-music-metadata-review-bang-approves-and-advances ()
  (let* ((first (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
         (second (apple-music-metadata-review-test--change
                  "N2" "needs_review" "Second" '((title . "Second new"))))
         (rows (list
                (apple-music-metadata-review-test--row
                 "N1" "needs_review" "First" "First new")
                (apple-music-metadata-review-test--row
                 "N2" "needs_review" "Second" "Second new"))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list first second) rows)
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (goto-char (point-min))
      (should
       (eq (lookup-key apple-music-metadata-review-mode-map (kbd "!"))
           #'apple-music-metadata-review-approve-and-next))
      (execute-kbd-macro (kbd "!"))
      (should
       (eq (alist-get
            'approved
            (car (apple-music-metadata-review--entry-by-id "N1")))
           t))
      (should (equal (tabulated-list-get-id) "N2"))
      (apple-music-metadata-review-show-details)
      (should
       (eq (lookup-key apple-music-metadata-review-detail-mode-map (kbd "!"))
           #'apple-music-metadata-review-approve-and-next))
      (execute-kbd-macro (kbd "!"))
      (should
       (with-current-buffer apple-music-metadata-review--overview-buffer
         (eq (alist-get
              'approved
              (car (apple-music-metadata-review--entry-by-id "N2")))
             t)))
      (should (equal apple-music-metadata-review--entry-id "N2")))))

(ert-deftest apple-music-metadata-review-approval-keeps-viewport-position ()
  (let* ((ids (mapcar (lambda (index) (format "N%02d" index))
                      (number-sequence 1 40)))
         (changes
          (mapcar
           (lambda (id)
             (apple-music-metadata-review-test--change
              id "needs_review" id `((title . ,(concat id " new")))))
           ids))
         (rows
          (mapcar
           (lambda (id)
             (apple-music-metadata-review-test--row
              id "needs_review" id (concat id " new")))
           ids)))
    (apple-music-metadata-review-test--with-fixture
        (fixture changes rows)
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (goto-char (point-min))
      (forward-line 19)
      (let* ((window (selected-window))
             (row-line (line-number-at-pos))
             (start
              (save-excursion
                (forward-line -5)
                (line-beginning-position))))
        (set-window-start window start)
        (redisplay t)
        (should (= (- row-line (line-number-at-pos (window-start window))) 5))
        (apple-music-metadata-review-approve)
        (redisplay t)
        (should
         (= (- (line-number-at-pos) (line-number-at-pos (window-start window)))
            5))))))

(ert-deftest apple-music-metadata-review-save-and-quit-key ()
  (let ((change (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
        (row (apple-music-metadata-review-test--row
              "N1" "needs_review" "First" "First new")))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let ((plan-file (plist-get fixture :plan-file)))
        (apple-music-metadata-review-open plan-file)
        (should
         (eq (lookup-key apple-music-metadata-review-mode-map (kbd "C-c C-c"))
             #'apple-music-metadata-review-save-and-quit))
        (goto-char (point-min))
        (apple-music-metadata-review-approve)
        (let ((overview (current-buffer)))
          (apple-music-metadata-review-show-details)
          (let ((detail (current-buffer)))
            (should
             (eq (lookup-key apple-music-metadata-review-detail-mode-map
                             (kbd "C-c C-c"))
                 #'apple-music-metadata-review-save-and-quit))
            (execute-kbd-macro (kbd "C-c C-c"))
            (should-not (buffer-live-p overview))
            (should-not (buffer-live-p detail))))
        (let* ((saved (apple-music-metadata-review-test--read-plan plan-file))
               (saved-change (aref (alist-get 'metadata_changes saved) 0)))
          (should (eq (alist-get 'approved saved-change) t)))))))

(ert-deftest apple-music-metadata-review-discard-and-quit-key ()
  (let ((change (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
        (row (apple-music-metadata-review-test--row
              "N1" "needs_review" "First" "First new")))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let ((plan-file (plist-get fixture :plan-file)))
        (apple-music-metadata-review-open plan-file)
        (should
         (eq (lookup-key apple-music-metadata-review-mode-map (kbd "C-c C-k"))
             #'apple-music-metadata-review-discard-and-quit))
        (should
         (eq (lookup-key apple-music-metadata-review-detail-mode-map
                         (kbd "C-c C-k"))
             #'apple-music-metadata-review-discard-and-quit))
        (goto-char (point-min))
        (apple-music-metadata-review-approve)
        (let ((overview (current-buffer)))
          (execute-kbd-macro (kbd "C-c C-k"))
          (should-not (buffer-live-p overview)))
        (let* ((saved (apple-music-metadata-review-test--read-plan plan-file))
               (saved-change (aref (alist-get 'metadata_changes saved) 0)))
          (should (eq (alist-get 'approved saved-change) :json-false)))))))

(ert-deftest apple-music-metadata-review-navigation-follows-visible-sort-order ()
  (let* ((first (apple-music-metadata-review-test--change
                 "N1" "needs_review" "First" '((title . "First new"))))
         (second (apple-music-metadata-review-test--change
                  "N2" "needs_review" "Second" '((title . "Second new"))))
         (rows (list
                (apple-music-metadata-review-test--row
                 "N1" "needs_review" "First" "First new")
                (apple-music-metadata-review-test--row
                 "N2" "needs_review" "Second" "Second new"))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list first second) rows)
      (apple-music-metadata-review-open (plist-get fixture :plan-file))
      (setq tabulated-list-sort-key '("Current title" . t))
      (apple-music-metadata-review--refresh)
      (goto-char (point-min))
      (should (equal (tabulated-list-get-id) "N2"))
      (apple-music-metadata-review-show-details)
      (apple-music-metadata-review-next)
      (should (equal apple-music-metadata-review--entry-id "N1"))
      (should (string-match-p "Metadata review 2 of 2" (buffer-string))))))

(ert-deftest apple-music-metadata-review-save-preserves-plan-and-backs-up ()
  (let* ((strong (apple-music-metadata-review-test--change
                  "S" "strong_candidate" "Strong" '((title . "Better"))))
         (review (apple-music-metadata-review-test--change
                  "N" "needs_review" "Beyoncé" '((title . "Beyoncé Knowles"))))
         (rows (list
                (apple-music-metadata-review-test--row
                 "S" "strong_candidate" "Strong" "Better")
                (apple-music-metadata-review-test--row
                 "N" "needs_review" "Beyoncé" "Beyoncé Knowles"))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list strong review) rows)
      (let* ((plan-file (plist-get fixture :plan-file))
             (backup-directory (expand-file-name "backups" directory))
             (backup-directory-alist `(("." . ,backup-directory)))
             (file-precious-flag nil))
        (make-directory backup-directory)
        (apple-music-metadata-review-open plan-file)
        (goto-char (point-min))
        (apple-music-metadata-review-show-details)
        (apple-music-metadata-review-approve)
        (apple-music-metadata-review-save)
        (let* ((text (with-temp-buffer
                       (insert-file-contents plan-file)
                       (buffer-string)))
               (saved (apple-music-metadata-review-test--read-plan plan-file))
               (changes (alist-get 'metadata_changes saved)))
          (should (string-match-p (regexp-quote "\n  \"snapshot\"") text))
          (should (string-match-p "Beyoncé" text))
          (should (string-suffix-p "\n" text))
          (should-not (string-suffix-p "\n\n" text))
          (should (equal (alist-get 'retained saved) '((nested . [1 2 3]))))
          (should (eq (alist-get 'approved (aref changes 0)) :json-false))
          (should (eq (alist-get 'approved (aref changes 1)) t))
          (should
           (file-exists-p
            (with-current-buffer apple-music-metadata-review--overview-buffer
              (with-current-buffer apple-music-metadata-review--source-buffer
                (make-backup-file-name buffer-file-name)))))
          (should-not file-precious-flag)
          (should
           (with-current-buffer apple-music-metadata-review--overview-buffer
             (with-current-buffer apple-music-metadata-review--source-buffer
               (and (local-variable-p 'file-precious-flag)
                    file-precious-flag))))
          (should-not (with-current-buffer
                          apple-music-metadata-review--overview-buffer
                        (buffer-modified-p))))))))

(ert-deftest apple-music-metadata-review-save-ignores-invalid-backup-directory ()
  (let ((change (apple-music-metadata-review-test--change
                 "N" "needs_review" "Old" '((title . "New"))))
        (row (apple-music-metadata-review-test--row
              "N" "needs_review" "Old" "New")))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let ((plan-file (plist-get fixture :plan-file))
            (backup-directory-alist
             '(("." . (expand-file-name "backups" user-emacs-directory))))
            (version-control 'never))
        (apple-music-metadata-review-open plan-file)
        (goto-char (point-min))
        (apple-music-metadata-review-approve)
        (apple-music-metadata-review-save)
        (let* ((saved (apple-music-metadata-review-test--read-plan plan-file))
               (saved-change (aref (alist-get 'metadata_changes saved) 0)))
          (should (eq (alist-get 'approved saved-change) t)))
        (should (file-exists-p (concat plan-file "~")))))))

(ert-deftest apple-music-metadata-review-save-keeps-empty-array ()
  (apple-music-metadata-review-test--with-fixture (fixture nil nil)
    (let ((plan-file (plist-get fixture :plan-file))
          (backup-directory-alist nil))
      (apple-music-metadata-review-open plan-file)
      (set-buffer-modified-p t)
      (save-buffer)
      (let ((text (with-temp-buffer
                    (insert-file-contents plan-file)
                    (buffer-string)))
            (saved (apple-music-metadata-review-test--read-plan plan-file)))
        (should (equal (alist-get 'metadata_changes saved) []))
        (should
         (string-match-p
          (regexp-quote "\"metadata_changes\": []")
          text))))))

(ert-deftest apple-music-metadata-review-open-rejects-bad-inputs ()
  (let ((change (apple-music-metadata-review-test--change
                 "N" "needs_review" "Old" '((title . "New")))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N" "needs_review" "Old" "New")))
      (delete-file (plist-get fixture :report))
      (let ((error-data
             (should-error
              (apple-music-metadata-review-open (plist-get fixture :plan-file)))))
        (should (string-prefix-p "Review report is not readable: "
                                 (error-message-string error-data)))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N" "needs_review" "Old" "New")
                       (apple-music-metadata-review-test--row
                        "N" "needs_review" "Old" "New")))
      (should-error
       (apple-music-metadata-review-open (plist-get fixture :plan-file)))
      :type 'error)
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N" "needs_review" "Different" "New")))
      (let ((error-data
             (should-error
              (apple-music-metadata-review-open (plist-get fixture :plan-file)))))
        (should
         (equal (error-message-string error-data)
                "Apply plan and review report disagree for persistent_id: N"))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change)
                 (list (apple-music-metadata-review-test--row
                        "N" "needs_review" "Old" "New")))
      (let ((source (find-file-noselect (plist-get fixture :plan-file))))
        (with-current-buffer source
          (goto-char (point-max))
          (insert " "))
        (let ((error-data
               (should-error
                (apple-music-metadata-review-open (plist-get fixture :plan-file)))))
          (should (equal (error-message-string error-data)
                         "Save or revert the apply plan before reviewing it")))))))

(ert-deftest apple-music-metadata-review-save-rejects-source-and-disk-changes ()
  (let ((change (apple-music-metadata-review-test--change
                 "N" "needs_review" "Old" '((title . "New"))))
        (row (apple-music-metadata-review-test--row
              "N" "needs_review" "Old" "New")))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let* ((plan-file (plist-get fixture :plan-file))
             (before (with-temp-buffer
                       (insert-file-contents plan-file)
                       (buffer-string))))
        (apple-music-metadata-review-open plan-file)
        (goto-char (point-min))
        (apple-music-metadata-review-approve)
        (with-current-buffer apple-music-metadata-review--source-buffer
          (goto-char (point-max))
          (insert " "))
        (let ((error-data (should-error (save-buffer))))
          (should (equal (error-message-string error-data)
                         "Apply plan changed; press g to reload before saving")))
        (should (equal before
                       (with-temp-buffer
                         (insert-file-contents plan-file)
                         (buffer-string))))))
    (apple-music-metadata-review-test--with-fixture
        (fixture (list change) (list row))
      (let* ((plan-file (plist-get fixture :plan-file))
             (external (concat
                        (with-temp-buffer
                          (insert-file-contents plan-file)
                          (buffer-string))
                        " ")))
        (apple-music-metadata-review-open plan-file)
        (goto-char (point-min))
        (apple-music-metadata-review-approve)
        (with-temp-file plan-file (insert external))
        (set-file-times plan-file (time-add (current-time) 2))
        (let ((error-data (should-error (save-buffer))))
          (should (equal (error-message-string error-data)
                         "Apply plan changed; press g to reload before saving")))
        (should (equal external
                       (with-temp-buffer
                         (insert-file-contents plan-file)
                         (buffer-string))))))))

;;; apple-music-metadata-review-test.el ends here
