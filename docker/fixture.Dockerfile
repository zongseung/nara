FROM nara-updater:prefect-3.9.0
COPY test/integration/fixture_api.py /fixture-api.py
RUN mkdir /fixture && printf '{"price":"100"}' > /fixture/state.json
CMD ["python", "/fixture-api.py"]
