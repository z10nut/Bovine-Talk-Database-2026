@echo on
set RUN_PARAM=%1

python combine.py --LFC ".\%RUN_PARAM%\outLFC.txt" --HFC ".\%RUN_PARAM%\outHFC.txt" --out ".\%RUN_PARAM%\\"
python moo.py -i ".\%RUN_PARAM%\HFC\\" -o ".\%RUN_PARAM%\output.csv" -a calf -c HFC --isolate --retries 20
python moo.py -i ".\%RUN_PARAM%\LFC\\" -o ".\%RUN_PARAM%\output.csv" -a calf -c LFC --isolate --retries 20 --append
python compare.py -orig ".\%RUN_PARAM%\%RUN_PARAM%.xlsx" -new ".\%RUN_PARAM%\output.csv"