;;; ------------------------------------------------------------------
;;; t2m.lsp
;;; Command: T2M
;;; Converts single-line text (TEXT / DTEXT) to MTEXT.
;;;
;;;   Select objects: (anything - only TEXT objects are converted)
;;;   Convert each text separately or join into one MTEXT?
;;;       [Separate/Join] <Separate>:
;;;   Delete original text? [Yes/No] <Yes>:
;;;
;;; Separate : one MTEXT per TEXT, same position, height, rotation,
;;;            justification, layer, color, linetype and text style.
;;;            Width factor and oblique angle are kept as \W and \Q codes.
;;; Join     : all selected text becomes ONE MTEXT, one paragraph per
;;;            text, ordered top to bottom (left to right on the same
;;;            row). Position, height, layer and style come from the
;;;            top-most text. Width factor and oblique are not carried over.
;;;
;;; Uses only core LISP functions (no vl-*). If CADCraft refuses to create
;;; the MTEXT, the originals are left untouched.
;;; Backslash and braces in the text are escaped so they stay literal.
;;; ------------------------------------------------------------------

;; True if s is an abbreviation of word with at least minlen characters
(defun t2m:kw (s word minlen)
  (and (>= (strlen s) minlen)
       (<= (strlen s) (strlen word))
       (= s (substr word 1 (strlen s))))
)

;; Value of a DXF group, or def if the group is missing
(defun t2m:val (ed code def)
  (if (assoc code ed) (cdr (assoc code ed)) def)
)

;; Escape \ { } so TEXT content is shown literally in MTEXT
(defun t2m:esc (s / out i c)
  (setq out "" i 1)
  (while (<= i (strlen s))
    (setq c (substr s i 1))
    (setq out (strcat out
                      (if (member c '("\\" "{" "}"))
                        (strcat "\\" c)
                        c))
          i (1+ i))
  )
  out
)

;; MTEXT attachment point (71) from TEXT horizontal (72) / vertical (73)
;;   MTEXT: 1 TL 2 TC 3 TR / 4 ML 5 MC 6 MR / 7 BL 8 BC 9 BR
;;   TEXT : h 0 left 1 center 2 right 3 aligned 4 middle 5 fit
;;          v 0 baseline 1 bottom 2 middle 3 top
(defun t2m:attach (h v)
  (cond
    ((= h 4) 5)
    ((member h '(3 5)) 7)
    (T (+ (cond ((= v 3) 0) ((= v 2) 3) (T 6)) (1+ h)))
  )
)

;; The point the TEXT is positioned by (alignment point when justified)
(defun t2m:point (ed h v)
  (if (or (and (= h 0) (= v 0)) (= h 3) (= h 5) (not (assoc 11 ed)))
    (cdr (assoc 10 ed))
    (cdr (assoc 11 ed))
  )
)

;; Build an MTEXT from the TEXT entity list ed.
;; txt = replacement contents (already escaped) or nil to use ed's own text.
;; Returns the entmake result (nil on failure).
(defun t2m:make (ed txt / h v pt att rot hgt wf ob new)
  (setq h   (t2m:val ed 72 0)
        v   (t2m:val ed 73 0)
        pt  (t2m:point ed h v)
        att (t2m:attach h v)
        hgt (t2m:val ed 40 2.5)
        rot (t2m:val ed 50 0.0))
  ;; aligned / fit text: rotation comes from the two end points
  (if (and (member h '(3 5)) (assoc 11 ed))
    (setq rot (angle (cdr (assoc 10 ed)) (cdr (assoc 11 ed)))))
  (if (not txt)
    (progn
      (setq txt (t2m:esc (t2m:val ed 1 ""))
            wf  (t2m:val ed 41 1.0)
            ob  (t2m:val ed 51 0.0))
      (if (/= ob 0.0) (setq txt (strcat "\\Q" (rtos ob 2 4) ";" txt)))
      (if (/= wf 1.0) (setq txt (strcat "\\W" (rtos wf 2 4) ";" txt)))
    )
  )
  (setq new (list '(0 . "MTEXT")
                  '(100 . "AcDbEntity")
                  (cons 8 (t2m:val ed 8 "0"))))
  (if (assoc 62 ed)  (setq new (append new (list (assoc 62 ed)))))
  (if (assoc 6 ed)   (setq new (append new (list (assoc 6 ed)))))
  (if (assoc 370 ed) (setq new (append new (list (assoc 370 ed)))))
  (setq new (append new
                    (list '(100 . "AcDbMText")
                          (cons 10 pt)
                          (cons 40 hgt)
                          (cons 41 0.0)
                          (cons 71 att)
                          (cons 72 1)
                          (cons 1 txt)
                          (cons 50 rot))))
  (if (assoc 7 ed)   (setq new (append new (list (assoc 7 ed)))))
  (if (assoc 210 ed) (setq new (append new (list (assoc 210 ed)))))
  (entmake new)
)

;; Remove the first element equal to item from lst
(defun t2m:remove (item lst / res done g)
  (foreach g lst
    (if (and (not done) (equal g item))
      (setq done T)
      (setq res (cons g res))))
  (reverse res)
)

;; Items are (y x ename). True if a comes before b: higher y first,
;; then lower x.
(defun t2m:before (a b)
  (or (> (car a) (car b))
      (and (= (car a) (car b)) (< (cadr a) (cadr b))))
)

(defun t2m:sort (lst / out best it)
  (while lst
    (setq best (car lst))
    (foreach it lst
      (if (t2m:before it best) (setq best it)))
    (setq out (cons best out)
          lst (t2m:remove best lst))
  )
  (reverse out)
)

(defun c:T2M (/ ss i ent ed ents n bad skip mode del ans items txt isfirst pt h v it)
  (setq ss (ssget))
  (if (not ss)
    (prompt "\nNothing selected.")
    (progn
      ;; pick out the TEXT objects
      (setq i 0 ents nil skip 0)
      (repeat (sslength ss)
        (setq ent (ssname ss i)
              i   (1+ i))
        (if (= (cdr (assoc 0 (entget ent))) "TEXT")
          (setq ents (cons ent ents))
          (setq skip (1+ skip))))
      (setq ents (reverse ents))
      (if (not ents)
        (prompt "\nNo TEXT objects in the selection.")
        (progn
          (setq ans (strcase (getstring
            "\nConvert each text separately or join into one MTEXT? [Separate/Join] <Separate>: ")))
          (setq mode (if (and (/= ans "") (t2m:kw ans "JOIN" 1)) "JOIN" "SEP"))
          (setq ans (strcase (getstring "\nDelete original text? [Yes/No] <Yes>: ")))
          (setq del (not (and (/= ans "") (t2m:kw ans "NO" 1))))
          (setq n 0 bad 0)
          (if (= mode "SEP")
            ;; ---- one MTEXT per TEXT -------------------------------
            (foreach ent ents
              (setq ed (entget ent))
              (if (t2m:make ed nil)
                (progn
                  (if del (entdel ent))
                  (setq n (1+ n)))
                (setq bad (1+ bad))))
            ;; ---- join everything into one MTEXT -------------------
            (progn
              (setq items nil)
              (foreach ent ents
                (setq ed (entget ent)
                      h  (t2m:val ed 72 0)
                      v  (t2m:val ed 73 0)
                      pt (t2m:point ed h v))
                (setq items (cons (list (cadr pt) (car pt) ent) items)))
              (setq items (t2m:sort items)
                    txt   ""
                    isfirst T)
              (foreach it items
                (setq ed (entget (caddr it)))
                (setq txt (if isfirst
                            (t2m:esc (t2m:val ed 1 ""))
                            (strcat txt "\\P" (t2m:esc (t2m:val ed 1 "")))))
                (setq isfirst nil))
              (setq ed (entget (caddr (car items))))
              (if (t2m:make ed txt)
                (progn
                  (if del
                    (foreach it items (entdel (caddr it))))
                  (setq n (length items)))
                (setq bad (length items)))
            )
          )
          (if (= mode "JOIN")
            (prompt (strcat "\n" (itoa n) " text object(s) joined into 1 MTEXT."))
            (prompt (strcat "\n" (itoa n) " text object(s) converted to MTEXT.")))
          (if (> bad 0)
            (prompt (strcat "\n" (itoa bad)
                            " could NOT be converted - CADCraft refused to create the MTEXT."
                            " The originals were left unchanged.")))
          (if (> skip 0)
            (prompt (strcat "\n" (itoa skip) " non-TEXT object(s) ignored.")))
        )
      )
    )
  )
  (princ)
)

(prompt "\nT2M loaded. Type T2M to convert TEXT/DTEXT to MTEXT.")
(princ)
