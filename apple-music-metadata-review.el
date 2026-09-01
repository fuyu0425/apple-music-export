;;; apple-music-metadata-review.el --- Review metadata apply plans  -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;; Author: Apple Music Export contributors
;; Package-Version: 0.1.0
;; Package-Requires: ((emacs "29.1"))
;; Keywords: multimedia

;;; Commentary:

;; Install this file with `M-x package-install-file`, then open an apply plan
;; with `M-x apple-music-metadata-review-open`.
;;
;; In the overview, use RET for details, SPC to toggle approval, `a` to
;; approve, `!` to approve and advance, `u` to unapprove, `g` to reload, and
;; C-x C-s to save.  In the detail buffer, `n` and `p` navigate, `q` closes
;; the detail window, and the approval and save keys have the same meaning.

;;; Code:

(require 'browse-url)
(require 'button)
(require 'cl-lib)
(require 'json)
(require 'seq)
(require 'subr-x)
(require 'tabulated-list)

(declare-function evil-define-key* "evil-core" (state keymap &rest bindings))
(declare-function evil-make-intercept-map "evil-core" (keymap &optional state aux))
(declare-function evil-normalize-keymaps "evil-core" ())
(declare-function evil-set-initial-state "evil-core" (mode state))

(defconst apple-music-metadata-review--report-columns
  '("persistent_id" "match_status"
    "current_title" "current_artist" "current_album"

    "suggested_title" "suggested_artist" "suggested_album"
    "reasons" "acoustid_id" "acoustid_score"
    "musicbrainz_recording_id" "musicbrainz_release_id"
    "beets_recommendation" "beets_distance" "distance_penalties"
    "evidence" "source_urls" "playlists" "duration_seconds" "location"))
(defconst apple-music-metadata-review--optional-report-columns
  '("cover_art_url"))

(defvar-local apple-music-metadata-review--plan-file nil)
(defvar-local apple-music-metadata-review--source-buffer nil)
(defvar-local apple-music-metadata-review--source-created-p nil)
(defvar-local apple-music-metadata-review--source-tick nil)
(defvar-local apple-music-metadata-review--plan nil)
(defvar-local apple-music-metadata-review--report-rows nil)
(defvar-local apple-music-metadata-review--entries nil)
(defvar-local apple-music-metadata-review--detail-buffer nil)
(defvar-local apple-music-metadata-review--backup-created-p nil)
(defvar-local apple-music-metadata-review--overview-buffer nil)
(defvar-local apple-music-metadata-review--entry-id nil)

(defun apple-music-metadata-review--csv-error (record message)
  (error "Malformed review CSV record %d: %s" record message))

(defun apple-music-metadata-review--parse-csv-string (text)
  "Parse CSV TEXT and return a list of records.
Each record is a list of fields.  Reject malformed quoting with its record
number.  Record separators and quoted CRLF sequences normalize to newlines."
  (let ((length (length text))
        (index 0)
        (record-number 1)
        (records nil)
        (record nil)
        (field nil)
        (state 'start))
    (cl-labels
        ((add-char (character) (push character field))
         (finish-field ()
           (push (apply #'string (nreverse field)) record)
           (setq field nil state 'start))
         (finish-record ()
           (finish-field)
           (push (nreverse record) records)
           (setq record nil)
           (cl-incf record-number))
         (newline-length ()
           (if (and (= (aref text index) ?\r)
                    (< (1+ index) length)
                    (= (aref text (1+ index)) ?\n))
               2
             1)))
      (while (< index length)
        (let ((character (aref text index)))
          (pcase state
            ('start
             (cond
              ((= character ?\") (setq state 'quoted))
              ((= character ?,) (finish-field))
              ((memq character '(?\r ?\n))
               (cl-incf index (1- (newline-length)))
               (finish-record))
              (t (add-char character) (setq state 'unquoted))))
            ('unquoted
             (cond
              ((= character ?\")
               (apple-music-metadata-review--csv-error
                record-number "quote inside unquoted field"))
              ((= character ?,) (finish-field))
              ((memq character '(?\r ?\n))
               (cl-incf index (1- (newline-length)))
               (finish-record))
              (t (add-char character))))
            ('quoted
             (cond
              ((= character ?\") (setq state 'after-quote))
              ((memq character '(?\r ?\n))
               (cl-incf index (1- (newline-length)))
               (add-char ?\n))
              (t (add-char character))))
            ('after-quote
             (cond
              ((= character ?\") (add-char character) (setq state 'quoted))
              ((= character ?,) (finish-field))
              ((memq character '(?\r ?\n))
               (cl-incf index (1- (newline-length)))
               (finish-record))
              (t
               (apple-music-metadata-review--csv-error
                record-number "text after closing quote")))))
          (cl-incf index)))
      (when (eq state 'quoted)
        (apple-music-metadata-review--csv-error
         record-number "unterminated quoted field"))
      (when (or record field (not (eq state 'start)))
        (finish-record))
      (nreverse records))))

(defun apple-music-metadata-review--required-pair (object key description)
  (or (and (listp object) (assq key object))
      (error "Apply plan %s is missing" description)))

(defun apple-music-metadata-review--validate-plan (plan)
  (unless (listp plan)
    (error "Apply plan must contain a JSON object"))
  (dolist (key '(snapshot audit review_report))
    (unless (stringp (cdr (apple-music-metadata-review--required-pair
                           plan key (symbol-name key))))
      (error "Apply plan %s must be a string" key)))
  (let ((report (alist-get 'review_report plan))
        (changes (cdr (apple-music-metadata-review--required-pair
                       plan 'metadata_changes "metadata_changes")))
        (ids (make-hash-table :test #'equal)))
    (unless (file-name-absolute-p report)
      (error "Apply plan review_report must be an absolute path"))
    (unless (vectorp changes)
      (error "Apply plan metadata_changes must be an array"))
    (seq-doseq (change changes)
      (unless (listp change)
        (error "Apply plan metadata change must be an object"))
      (let* ((id-pair (apple-music-metadata-review--required-pair
                       change 'persistent_id "persistent_id"))
             (id (cdr id-pair))
             (approved (cdr (apple-music-metadata-review--required-pair
                             change 'approved "approved")))
             (status (cdr (apple-music-metadata-review--required-pair
                           change 'match_status "match_status")))
             (current (cdr (apple-music-metadata-review--required-pair
                            change 'current "current")))
             (suggested (cdr (apple-music-metadata-review--required-pair
                              change 'suggested "suggested")))
             (artwork (alist-get 'artwork change)))
        (unless (and (stringp id) (not (string-empty-p id)))
          (error "Apply plan persistent_id must be a nonempty string"))
        (when (gethash id ids)
          (error "Duplicate apply plan persistent_id: %s" id))
        (puthash id t ids)
        (unless (memq approved '(t :json-false))
          (error "Apply plan approved must be a boolean for persistent_id: %s" id))
        (unless (and (stringp status)
                     (member status '("strong_candidate" "needs_review")))
          (error "Apply plan match_status is invalid for persistent_id: %s" id))
        (unless (and (listp current)
                     (cl-every (lambda (pair) (stringp (cdr pair))) current)
                     (cl-every (lambda (key) (stringp (alist-get key current)))
                               '(title artist album)))
          (error "Apply plan current metadata is invalid for persistent_id: %s" id))
        (unless (and (listp suggested)
                     (cl-every (lambda (pair)
                                 (and (memq (car pair) '(title artist album))
                                      (stringp (cdr pair))))
                               suggested)
                     (seq-some (lambda (key)
                                 (let ((value (alist-get key suggested)))
                                   (and (stringp value) (not (string-empty-p value)))))
                               '(title artist album)))
          (error "Apply plan suggested metadata is invalid for persistent_id: %s" id))
        (when artwork
          (unless
              (and (listp artwork)
                   (= (length artwork) 3)
                   (cl-every
                    (lambda (pair)
                      (memq (car pair) '(source release_id url)))
                    artwork)
                   (equal (alist-get 'source artwork) "cover_art_archive")
                   (let ((release-id (alist-get 'release_id artwork)))
                     (and (stringp release-id)
                          (not (string-empty-p release-id))))
                   (let ((url (alist-get 'url artwork)))
                     (and (stringp url)
                          (string-match-p "\\`https://" url))))
            (error "Apply plan artwork is invalid for persistent_id: %s" id)))))
    plan))

(defun apple-music-metadata-review--parse-plan-buffer (source-buffer)
  (with-current-buffer source-buffer
    (save-restriction
      (widen)
      (goto-char (point-min))
      (let ((plan (json-parse-buffer
                   :object-type 'alist
                   :array-type 'array
                   :null-object :json-null
                   :false-object :json-false)))
        (skip-chars-forward " \t\r\n")
        (unless (eobp)
          (error "Apply plan has trailing content"))
        (apple-music-metadata-review--validate-plan plan)))))

(defun apple-music-metadata-review--row-value (row column)
  (cdr (assoc column row)))

(defun apple-music-metadata-review--read-report (path)
  (unless (file-readable-p path)
    (error "Review report is not readable: %s" path))
  (let ((records
         (with-temp-buffer
           (insert-file-contents path)
           (apple-music-metadata-review--parse-csv-string (buffer-string)))))
    (unless records
      (error "Malformed review CSV record 1: missing header"))
    (let* ((header (car records))
           (width (length header))
           (positions (make-hash-table :test #'equal))
           (rows (make-hash-table :test #'equal)))
      (cl-loop for column in header
               for index from 0
               do (push index (gethash column positions)))
      (dolist (column apple-music-metadata-review--report-columns)
        (unless (= (length (gethash column positions)) 1)
          (error "Malformed review CSV record 1: column %s must occur once" column)))
      (dolist (column apple-music-metadata-review--optional-report-columns)
        (when (> (length (gethash column positions)) 1)
          (error "Malformed review CSV record 1: column %s occurs more than once"
                 column)))
      (cl-loop for fields in (cdr records)
               for record-number from 2
               do
               (unless (= (length fields) width)
                 (apple-music-metadata-review--csv-error
                  record-number
                  (format "expected %d fields, got %d" width (length fields))))
               (let* ((row (cl-mapcar #'cons header fields))
                      (row (if (member "cover_art_url" header)
                               row
                             (cons '("cover_art_url" . "") row)))
                      (id (apple-music-metadata-review--row-value
                           row "persistent_id")))
                 (when (gethash id rows)
                   (error "Duplicate review CSV persistent_id: %s" id))
                 (puthash id row rows)))
      rows)))

(defun apple-music-metadata-review--join-review-set (plan rows)
  (let (entries)
    (seq-doseq (change (alist-get 'metadata_changes plan))
      (when (equal (alist-get 'match_status change) "needs_review")
        (let* ((id (alist-get 'persistent_id change))
               (row (gethash id rows))
               (current (alist-get 'current change))
               (suggested (alist-get 'suggested change))
               (artwork (alist-get 'artwork change))
               (cover-art-url
                (apple-music-metadata-review--row-value row "cover_art_url")))
          (unless (and row
                       (equal (apple-music-metadata-review--row-value
                               row "match_status") "needs_review")
                       (cl-every
                        (lambda (field)
                          (equal (alist-get field current)
                                 (apple-music-metadata-review--row-value
                                  row (format "current_%s" field))))
                        '(title artist album))
                       (cl-every
                        (lambda (field)
                          (equal (or (alist-get field suggested) "")
                                 (apple-music-metadata-review--row-value
                                  row (format "suggested_%s" field))))
                        '(title artist album))
                       (if artwork
                           (and
                            (equal (alist-get 'url artwork) cover-art-url)
                            (equal
                             (alist-get 'release_id artwork)
                             (apple-music-metadata-review--row-value
                              row "musicbrainz_release_id")))
                         (string-empty-p cover-art-url)))
            (error "Apply plan and review report disagree for persistent_id: %s" id))
          (push (cons change row) entries))))
    (nreverse entries)))

(defun apple-music-metadata-review--load-review-set (source-buffer)
  (let* ((plan (apple-music-metadata-review--parse-plan-buffer source-buffer))
         (rows (apple-music-metadata-review--read-report
                (alist-get 'review_report plan)))
         (entries (apple-music-metadata-review--join-review-set plan rows)))
    (list :plan plan :report-rows rows :entries entries)))

(defun apple-music-metadata-review--review-buffer-for-file (plan-file)
  (seq-find
   (lambda (buffer)
     (with-current-buffer buffer
       (and (derived-mode-p 'apple-music-metadata-review-mode)
            (equal apple-music-metadata-review--plan-file plan-file))))
   (buffer-list)))

(defun apple-music-metadata-review--display-current (value)
  (if (string-empty-p value)
      (propertize "(empty)" 'face 'shadow)
    value))

(defun apple-music-metadata-review--display-suggested (change field)
  (let ((suggested (alist-get field (alist-get 'suggested change))))
    (if (and (stringp suggested) (not (string-empty-p suggested)))
        (propertize suggested 'face 'success)
      (propertize "(not suggested)" 'face 'shadow))))

(defun apple-music-metadata-review--truncate-to-pixels (string max-pixels)
  (if (<= (string-pixel-width string) max-pixels)
      string
    (let* ((ellipsis (copy-sequence (or truncate-string-ellipsis "…")))
           (properties (and (not (string-empty-p string))
                            (text-properties-at 0 string)))
           (low 0)
           (high (length string)))
      (when properties
        (set-text-properties 0 (length ellipsis) properties ellipsis))
      (while (< low high)
        (let ((middle (/ (+ low high 1) 2)))
          (if (<= (string-pixel-width
                   (concat (substring string 0 middle) ellipsis))
                  max-pixels)
              (setq low middle)
            (setq high (1- middle)))))
      (concat (substring string 0 low) ellipsis))))

(defun apple-music-metadata-review--print-entry (id columns)
  (if (not (display-graphic-p))
      (tabulated-list-print-entry id columns)
    (let* ((beginning (point))
           (space-width
            (max 1 (string-pixel-width " ")))
           (x (* (max tabulated-list-padding 0) space-width))
           (column-count (length tabulated-list-format))
           (inhibit-read-only t))
      (when (> x 0)
        (insert
         (propertize " " 'display `(space :align-to (,x)))))
      (dotimes (index column-count)
        (let* ((format (aref tabulated-list-format index))
               (name (nth 0 format))
               (width (nth 1 format))
               (padding (or (plist-get (nthcdr 3 format) :pad-right) 1))
               (last (= index (1- column-count)))
               (label (aref columns index))
               (column-beginning (point))
               (rendered
                (if last
                    label
                  (apple-music-metadata-review--truncate-to-pixels
                   label (* width space-width))))
               (next-x (+ x (* (+ width padding) space-width))))
          (insert rendered)
          (add-text-properties
           column-beginning (point)
           `(help-echo ,(concat name ": " label)))
          (unless last
            (insert
             (propertize " " 'display `(space :align-to (,next-x)))))
          (put-text-property
           column-beginning (point) 'tabulated-list-column-name name)
          (setq x next-x)))
      (insert ?\n)
      (add-text-properties
       beginning (point)
       `(tabulated-list-id ,id tabulated-list-entry ,columns)))))

(defun apple-music-metadata-review--tabulated-entries ()
  (mapcar
   (lambda (entry)
     (let* ((change (car entry))
            (current (alist-get 'current change)))
       (list
        (alist-get 'persistent_id change)
        (vector
         (if (eq (alist-get 'approved change) t) "[x]" "[ ]")
         (apple-music-metadata-review--display-current
          (alist-get 'title current))
         (apple-music-metadata-review--display-suggested change 'title)
         (apple-music-metadata-review--display-current
          (alist-get 'artist current))
         (apple-music-metadata-review--display-suggested change 'artist)
         (apple-music-metadata-review--display-current
          (alist-get 'album current))
         (apple-music-metadata-review--display-suggested change 'album)))))
   apple-music-metadata-review--entries))

(defun apple-music-metadata-review--entry-by-id (id)
  (seq-find
   (lambda (entry) (equal (alist-get 'persistent_id (car entry)) id))
   apple-music-metadata-review--entries))

(defun apple-music-metadata-review--goto-id (id)
  (goto-char (point-min))
  (when id
    (catch 'found
      (while (not (eobp))
        (when (equal (tabulated-list-get-id) id)
          (throw 'found t))
        (forward-line 1)))))

(defun apple-music-metadata-review--displayed-ids ()
  (save-excursion
    (goto-char (point-min))
    (let (ids)
      (while (not (eobp))
        (when-let* ((id (tabulated-list-get-id)))
          (push id ids))
        (forward-line 1))
      (nreverse ids))))

(defun apple-music-metadata-review--refresh (&optional keep-id)
  (let* ((modified (buffer-modified-p))
         (window (get-buffer-window (current-buffer) t))
         (viewport-line
          (and window
               (- (line-number-at-pos)
                  (line-number-at-pos (window-start window)))))
         (approved
          (seq-count
           (lambda (entry) (eq (alist-get 'approved (car entry)) t))
           apple-music-metadata-review--entries))
         (total (length apple-music-metadata-review--entries))
         (progress
          (format "Needs review: %d  Approved: %d  Remaining: %d"
                  total approved (- total approved))))
    (setq mode-line-process (list "  " progress)
          tabulated-list-entries
          #'apple-music-metadata-review--tabulated-entries)
    (tabulated-list-init-header)
    (tabulated-list-print t)
    (apple-music-metadata-review--goto-id keep-id)
    (when (and window
               (window-live-p window)
               (eq (window-buffer window) (current-buffer)))
      (set-window-start
       window
       (save-excursion
         (forward-line (- viewport-line))
         (line-beginning-position))))
    (set-buffer-modified-p modified)))

(defun apple-music-metadata-review--overview ()
  (if (derived-mode-p 'apple-music-metadata-review-detail-mode)
      (or (and (buffer-live-p apple-music-metadata-review--overview-buffer)
               apple-music-metadata-review--overview-buffer)
          (error "The metadata review overview is no longer open"))
    (current-buffer)))

(defun apple-music-metadata-review--selected-id ()
  (if (derived-mode-p 'apple-music-metadata-review-detail-mode)
      apple-music-metadata-review--entry-id
    (or (tabulated-list-get-id)
        (error "No needs-review entry at point"))))

(defun apple-music-metadata-review--refresh-detail-for-id (id)
  (when (and (buffer-live-p apple-music-metadata-review--detail-buffer)
             (with-current-buffer apple-music-metadata-review--detail-buffer
               (equal apple-music-metadata-review--entry-id id)))
    (with-current-buffer apple-music-metadata-review--detail-buffer
      (apple-music-metadata-review--render-detail))))

(defun apple-music-metadata-review--set-approved (approved)
  (let* ((overview (apple-music-metadata-review--overview))
         (id (apple-music-metadata-review--selected-id)))
    (with-current-buffer overview
      (let* ((entry (or (apple-music-metadata-review--entry-by-id id)
                        (error "No needs-review entry at point")))
             (change (car entry))
             (pair (assq 'approved change)))
        (unless (eq (cdr pair) approved)
          (setcdr pair approved)
          (apple-music-metadata-review--refresh id)
          (set-buffer-modified-p t)
          (apple-music-metadata-review--refresh-detail-for-id id))))))

(defun apple-music-metadata-review-approve ()
  "Approve the selected metadata change."
  (interactive)
  (apple-music-metadata-review--set-approved t))

(defun apple-music-metadata-review-unapprove ()
  "Unapprove the selected metadata change."
  (interactive)
  (apple-music-metadata-review--set-approved :json-false))

(defun apple-music-metadata-review-toggle ()
  "Toggle approval for the selected metadata change."
  (interactive)
  (let* ((overview (apple-music-metadata-review--overview))
         (id (apple-music-metadata-review--selected-id))
         (approved
          (with-current-buffer overview
            (eq (alist-get 'approved
                           (car (apple-music-metadata-review--entry-by-id id)))
                t))))
    (apple-music-metadata-review--set-approved
     (if approved :json-false t))))

(defun apple-music-metadata-review--serialized-plan ()
  (let ((plan apple-music-metadata-review--plan))
    (with-temp-buffer
      (insert
       (decode-coding-string
        (json-serialize plan
                        :null-object :json-null
                        :false-object :json-false)
        'utf-8))
      (json-pretty-print-buffer)
      (goto-char (point-max))
      (skip-chars-backward " \t\r\n")
      (delete-region (point) (point-max))
      (insert "\n")
      (buffer-string))))

(defun apple-music-metadata-review--assert-source-unchanged ()
  (let ((source apple-music-metadata-review--source-buffer)
        (tick apple-music-metadata-review--source-tick))
    (unless (and (buffer-live-p source)
                 (with-current-buffer source
                   (and (= tick (buffer-chars-modified-tick))
                        (verify-visited-file-modtime source))))
      (error "Apply plan changed; press g to reload before saving"))))

(defun apple-music-metadata-review--create-backup ()
  (unless apple-music-metadata-review--backup-created-p
    (with-current-buffer apple-music-metadata-review--source-buffer
      (setq-local file-precious-flag t)
      (setq buffer-backed-up nil)
      (let ((make-backup-files t)
            (vc-make-backup-files t)
            (backup-inhibited nil)
            (backup-directory-alist
             (and
              (listp backup-directory-alist)
              (seq-filter
               (lambda (entry)
                 (and (consp entry)
                      (stringp (car entry))
                      (stringp (cdr entry))))
               backup-directory-alist))))
        (backup-buffer)))
    (setq apple-music-metadata-review--backup-created-p t)))

(defun apple-music-metadata-review--write-plan ()
  (apple-music-metadata-review--assert-source-unchanged)
  (let* ((overview (current-buffer))
         (source apple-music-metadata-review--source-buffer)
         (keep-id (tabulated-list-get-id))
         (serialized (apple-music-metadata-review--serialized-plan)))
    (apple-music-metadata-review--create-backup)
    (with-current-buffer source
      (let ((old-coding buffer-file-coding-system))
        (condition-case error-data
            (atomic-change-group
              (erase-buffer)
              (insert serialized)
              (setq buffer-file-coding-system 'utf-8-unix)
              (setq-local file-precious-flag t)
              (save-buffer))
          (error
           (setq buffer-file-coding-system old-coding)
           (signal (car error-data) (cdr error-data))))))
    (setq apple-music-metadata-review--source-tick
          (with-current-buffer source (buffer-chars-modified-tick)))
    (set-buffer-modified-p nil)
    (apple-music-metadata-review--refresh keep-id)
    (when (buffer-live-p apple-music-metadata-review--detail-buffer)
      (with-current-buffer apple-music-metadata-review--detail-buffer
        (apple-music-metadata-review--render-detail)))
    (with-current-buffer overview
      (set-buffer-modified-p nil))
    t))

(defun apple-music-metadata-review-save ()
  "Save approvals through the owning overview."
  (interactive)
  (let ((overview (apple-music-metadata-review--overview)))
    (with-current-buffer overview
      (if (buffer-modified-p)
          (apple-music-metadata-review--write-plan)
        (message "No approval changes to save")))))

(defun apple-music-metadata-review-save-and-quit ()
  "Save approvals and close the review buffers."
  (interactive)
  (let ((overview (apple-music-metadata-review--overview)))
    (with-current-buffer overview
      (apple-music-metadata-review-save)
      (kill-buffer overview))))

(defun apple-music-metadata-review-discard-and-quit ()
  "Discard unsaved approvals and close the review buffers."
  (interactive)
  (let ((overview (apple-music-metadata-review--overview)))
    (with-current-buffer overview
      (set-buffer-modified-p nil)
      (kill-buffer overview))))

(defun apple-music-metadata-review-reload ()
  "Reload the apply plan and its linked report from disk."
  (interactive)
  (let ((overview (apple-music-metadata-review--overview)))
    (with-current-buffer overview
      (when (and (buffer-modified-p)
                 (not (y-or-n-p "Discard unsaved approval changes?")))
        (user-error "Reload canceled"))
      (unless (buffer-live-p apple-music-metadata-review--source-buffer)
        (error "Apply plan changed; press g to reload before saving"))
      (let* ((source apple-music-metadata-review--source-buffer)
             (old-id (tabulated-list-get-id))
             loaded)
        (with-current-buffer source
          (revert-buffer t t))
        (setq loaded (apple-music-metadata-review--load-review-set source)
              apple-music-metadata-review--source-tick
              (with-current-buffer source (buffer-chars-modified-tick))
              apple-music-metadata-review--plan (plist-get loaded :plan)
              apple-music-metadata-review--report-rows
              (plist-get loaded :report-rows)
              apple-music-metadata-review--entries
              (plist-get loaded :entries))
        (set-buffer-modified-p nil)
        (let ((keep-id
               (if (apple-music-metadata-review--entry-by-id old-id)
                   old-id
                 (and apple-music-metadata-review--entries
                      (alist-get
                       'persistent_id
                       (car (car apple-music-metadata-review--entries)))))))
          (apple-music-metadata-review--refresh keep-id)
          (when (and keep-id
                     (buffer-live-p apple-music-metadata-review--detail-buffer))
            (with-current-buffer apple-music-metadata-review--detail-buffer
              (setq apple-music-metadata-review--entry-id keep-id)
              (apple-music-metadata-review--render-detail))))))))

(defun apple-music-metadata-review--kill-query ()
  (or (not (buffer-modified-p))
      (y-or-n-p "Discard unsaved approval changes?")))

(defun apple-music-metadata-review--cleanup ()
  (when (buffer-live-p apple-music-metadata-review--detail-buffer)
    (kill-buffer apple-music-metadata-review--detail-buffer))
  (when (and apple-music-metadata-review--source-created-p
             (buffer-live-p apple-music-metadata-review--source-buffer)
             (not (buffer-modified-p apple-music-metadata-review--source-buffer)))
    (kill-buffer apple-music-metadata-review--source-buffer)))

(defvar-keymap apple-music-metadata-review-mode-map
  :parent tabulated-list-mode-map
  "RET" #'apple-music-metadata-review-show-details
  "SPC" #'apple-music-metadata-review-toggle
  "C-x C-s" #'apple-music-metadata-review-save
  "C-c C-c" #'apple-music-metadata-review-save-and-quit
  "C-c C-k" #'apple-music-metadata-review-discard-and-quit
  "a" #'apple-music-metadata-review-approve
  "!" #'apple-music-metadata-review-approve-and-next
  "u" #'apple-music-metadata-review-unapprove
  "g" #'apple-music-metadata-review-reload)

(define-derived-mode apple-music-metadata-review-mode tabulated-list-mode
  "Metadata Review"
  "Review needs-review entries in an Apple Music metadata apply plan."
  (setq tabulated-list-format
        [("Approved" 10 t)
         ("Current title" 25 t)
         ("Suggested title" 25 t)
         ("Current artist" 21 t)
         ("Suggested artist" 21 t)
         ("Current album" 20 t)
         ("Suggested album" 20 t)]
        tabulated-list-padding 2
        tabulated-list-sort-key nil
        tabulated-list-printer #'apple-music-metadata-review--print-entry
        buffer-offer-save 'always)
  (add-hook 'kill-buffer-query-functions
            #'apple-music-metadata-review--kill-query nil t)
  (add-hook 'kill-buffer-hook #'apple-music-metadata-review--cleanup nil t)
  (add-hook 'write-contents-functions
            #'apple-music-metadata-review--write-plan nil t)
  (tabulated-list-init-header))

(defconst apple-music-metadata-review--detail-fields
  '(("Reasons" . "reasons")
    ("Beets recommendation" . "beets_recommendation")
    ("Beets distance" . "beets_distance")
    ("Distance penalties" . "distance_penalties")
    ("AcoustID ID" . "acoustid_id")
    ("AcoustID score" . "acoustid_score")
    ("MusicBrainz recording ID" . "musicbrainz_recording_id")
    ("MusicBrainz release ID" . "musicbrainz_release_id")
    ("Cover art URL" . "cover_art_url")
    ("Evidence" . "evidence")
    ("Playlists" . "playlists")
    ("Duration" . "duration_seconds")
    ("Location" . "location")))

(defun apple-music-metadata-review--insert-value (value &optional face)
  (if (string-empty-p value)
      (insert (propertize "(empty)" 'face 'shadow))
    (insert (if face (propertize value 'face face) value))))

(defun apple-music-metadata-review--render-detail ()
  (let* ((overview apple-music-metadata-review--overview-buffer)
         (id apple-music-metadata-review--entry-id)
         (entry
          (and (buffer-live-p overview)
               (with-current-buffer overview
                 (apple-music-metadata-review--entry-by-id id)))))
    (unless entry
      (error "No needs-review entry at point"))
    (let* ((change (car entry))
           (row (cdr entry))
           (current (alist-get 'current change))
           (suggested (alist-get 'suggested change))
           (artwork (alist-get 'artwork change))
           (queue
            (with-current-buffer overview
              (apple-music-metadata-review--displayed-ids)))
           (position (1+ (seq-position queue id #'equal)))
           (total (length queue))
           (approved (eq (alist-get 'approved change) t))
           (inhibit-read-only t))
      (erase-buffer)
      (insert (format "Metadata review %d of %d\n\n" position total))
      (insert "Status: ")
      (insert (propertize (if approved "Approved" "Not approved")
                          'face (if approved 'success 'warning)))
      (insert "\nPersistent ID: " id "\n\n")
      (insert (format "%-10s %-38s %s\n" "Field" "Current" "Suggested"))
      (insert (make-string 78 ?-) "\n")
      (dolist (field '(title artist album))
        (let ((current-value (alist-get field current))
              (suggested-value (alist-get field suggested)))
          (insert (format "%-10s " (capitalize (symbol-name field))))
          (let ((start (point)))
            (apple-music-metadata-review--insert-value current-value)
            (insert (make-string (max 1 (- 39 (- (point) start))) ?\s)))
          (cond
           ((and (stringp suggested-value)
                 (not (equal suggested-value current-value)))
            (apple-music-metadata-review--insert-value suggested-value 'success))
           ((stringp suggested-value)
            (insert (propertize "(unchanged)" 'face 'shadow)))
           (t
            (insert (propertize "(not suggested)" 'face 'shadow))))
          (insert "\n")))
      (insert (format "%-10s " "Artwork"))
      (let ((start (point)))
        (apple-music-metadata-review--insert-value "(checked during apply)")
        (insert (make-string (max 1 (- 39 (- (point) start))) ?\s)))
      (if artwork
          (apple-music-metadata-review--insert-value
           "Add exact release front if missing" 'success)
        (insert (propertize "(not suggested)" 'face 'shadow)))
      (insert "\n")
      (insert "\nEvidence\n")
      (insert (make-string 8 ?-) "\n")
      (dolist (field apple-music-metadata-review--detail-fields)
        (insert (car field) ": ")
        (apple-music-metadata-review--insert-value
         (apple-music-metadata-review--row-value row (cdr field)))
        (insert "\n"))
      (insert "Source URLs:\n")
      (let ((urls
             (split-string
              (apple-music-metadata-review--row-value row "source_urls")
              (regexp-quote " | ") t)))
        (if urls
            (dolist (url urls)
              (insert "  ")
              (insert-text-button
               url
               'follow-link t
               'url url
               'action
               (lambda (button) (browse-url (button-get button 'url))))
              (insert "\n"))
          (insert "  " (propertize "(empty)" 'face 'shadow) "\n")))
      (goto-char (point-min)))))

(defun apple-music-metadata-review-show-details ()
  "Show evidence for the needs-review entry at point."
  (interactive)
  (let* ((overview (apple-music-metadata-review--overview))
         (id (apple-music-metadata-review--selected-id))
         (detail
          (with-current-buffer overview
            (if (buffer-live-p apple-music-metadata-review--detail-buffer)
                apple-music-metadata-review--detail-buffer
              (setq apple-music-metadata-review--detail-buffer
                    (generate-new-buffer "*Apple Music Metadata Detail*"))))))
    (with-current-buffer detail
      (unless (derived-mode-p 'apple-music-metadata-review-detail-mode)
        (apple-music-metadata-review-detail-mode))
      (setq apple-music-metadata-review--overview-buffer overview
            apple-music-metadata-review--entry-id id)
      (apple-music-metadata-review--render-detail))
    (pop-to-buffer detail)))

(defun apple-music-metadata-review--navigate (offset)
  (let* ((detail (derived-mode-p 'apple-music-metadata-review-detail-mode))
         (overview (apple-music-metadata-review--overview))
         (id (apple-music-metadata-review--selected-id))
         (ids
          (with-current-buffer overview
            (apple-music-metadata-review--displayed-ids)))
         (position (seq-position ids id #'equal))
         (target (+ position offset)))
    (if (or (< target 0) (>= target (length ids)))
        (message "%s needs-review entry"
                 (if (< target 0) "First" "Last"))
      (let ((target-id (nth target ids)))
        (with-current-buffer overview
          (apple-music-metadata-review--goto-id target-id))
        (when detail
          (setq apple-music-metadata-review--entry-id target-id)
          (apple-music-metadata-review--render-detail))))))

(defun apple-music-metadata-review-next ()
  "Show the next needs-review entry without wrapping."
  (interactive)
  (apple-music-metadata-review--navigate 1))

(defun apple-music-metadata-review-previous ()
  "Show the previous needs-review entry without wrapping."
  (interactive)
  (apple-music-metadata-review--navigate -1))

(defun apple-music-metadata-review-approve-and-next ()
  "Approve the selected metadata change and advance one entry."
  (interactive)
  (apple-music-metadata-review-approve)
  (apple-music-metadata-review--navigate 1))

(defun apple-music-metadata-review-quit-detail ()
  "Close only the detail window."
  (interactive)
  (quit-window nil))

(defvar-keymap apple-music-metadata-review-detail-mode-map
  :parent special-mode-map
  "SPC" #'apple-music-metadata-review-toggle
  "a" #'apple-music-metadata-review-approve
  "!" #'apple-music-metadata-review-approve-and-next
  "u" #'apple-music-metadata-review-unapprove
  "n" #'apple-music-metadata-review-next
  "p" #'apple-music-metadata-review-previous
  "C-x C-s" #'apple-music-metadata-review-save
  "C-c C-c" #'apple-music-metadata-review-save-and-quit
  "C-c C-k" #'apple-music-metadata-review-discard-and-quit
  "q" #'apple-music-metadata-review-quit-detail)

(define-derived-mode apple-music-metadata-review-detail-mode special-mode
  "Metadata Review Detail"
  "Show evidence for one metadata review entry.")

(defconst apple-music-metadata-review--evil-overview-bindings
  '(("RET" . apple-music-metadata-review-show-details)
    ("SPC" . apple-music-metadata-review-toggle)
    ("a" . apple-music-metadata-review-approve)
    ("!" . apple-music-metadata-review-approve-and-next)
    ("u" . apple-music-metadata-review-unapprove)
    ("gr" . apple-music-metadata-review-reload)
    ("C-x C-s" . apple-music-metadata-review-save)))

(defconst apple-music-metadata-review--evil-detail-bindings
  '(("SPC" . apple-music-metadata-review-toggle)
    ("a" . apple-music-metadata-review-approve)
    ("u" . apple-music-metadata-review-unapprove)
    ("!" . apple-music-metadata-review-approve-and-next)
    ("n" . apple-music-metadata-review-next)
    ("p" . apple-music-metadata-review-previous)
    ("C-x C-s" . apple-music-metadata-review-save)
    ("q" . apple-music-metadata-review-quit-detail)))


(defun apple-music-metadata-review--configure-evil ()
  (remove-hook 'after-change-major-mode-hook
               #'apple-music-metadata-review--enable-evil-local-bindings)
  (evil-set-initial-state 'apple-music-metadata-review-mode 'motion)
  (evil-set-initial-state 'apple-music-metadata-review-detail-mode 'motion)
  (evil-define-key*
    'motion apple-music-metadata-review-mode-map
    (kbd "g") (make-sparse-keymap))
  (dolist (binding apple-music-metadata-review--evil-overview-bindings)
    (evil-define-key*
      'motion apple-music-metadata-review-mode-map
      (kbd (car binding)) (cdr binding)))
  (dolist (binding apple-music-metadata-review--evil-detail-bindings)
    (evil-define-key*
      'motion apple-music-metadata-review-detail-mode-map
      (kbd (car binding)) (cdr binding)))
  (evil-make-intercept-map
   apple-music-metadata-review-mode-map 'motion t)
  (evil-make-intercept-map
   apple-music-metadata-review-detail-mode-map 'motion t)
  (dolist (buffer (buffer-list))
    (with-current-buffer buffer
      (when (derived-mode-p 'apple-music-metadata-review-mode
                            'apple-music-metadata-review-detail-mode)
        (evil-normalize-keymaps)))))

(if (featurep 'evil)
    (apple-music-metadata-review--configure-evil)
  (with-eval-after-load 'evil
    (apple-music-metadata-review--configure-evil)))

;;;###autoload
(defun apple-music-metadata-review-open (plan-file)
  "Open PLAN-FILE for metadata review."
  (interactive "fApply plan: ")
  (let* ((normalized (file-truename (expand-file-name plan-file)))
         (existing (apple-music-metadata-review--review-buffer-for-file normalized)))
    (if existing
        (pop-to-buffer existing)
      (let* ((visiting (find-buffer-visiting normalized))
             (source (find-file-noselect normalized))
             (created (null visiting)))
        (when (buffer-modified-p source)
          (error "Save or revert the apply plan before reviewing it"))
        (condition-case error-data
            (let* ((loaded (apple-music-metadata-review--load-review-set source))
                   (review (generate-new-buffer
                            (format "*Apple Music Metadata Review: %s*"
                                    (file-name-nondirectory normalized)))))
              (with-current-buffer review
                (apple-music-metadata-review-mode)
                (setq apple-music-metadata-review--plan-file normalized
                      apple-music-metadata-review--source-buffer source
                      apple-music-metadata-review--source-created-p created
                      apple-music-metadata-review--source-tick
                      (with-current-buffer source (buffer-chars-modified-tick))
                      apple-music-metadata-review--plan (plist-get loaded :plan)
                      apple-music-metadata-review--report-rows
                      (plist-get loaded :report-rows)
                      apple-music-metadata-review--entries
                      (plist-get loaded :entries))
                (apple-music-metadata-review--refresh))
              (pop-to-buffer review))
          (error
           (when (and created (buffer-live-p source)
                      (not (buffer-modified-p source)))
             (kill-buffer source))
           (signal (car error-data) (cdr error-data))))))))

(provide 'apple-music-metadata-review)

;;; apple-music-metadata-review.el ends here
