# blitz-stats Dockerfile

## THIS IS BROKEN ##################

FROM python:3.13.0b3-slim

ENV PATH /app/blitz-stats:/usr/local/bin:$PATH
# extra dependencies (over what buildpack-deps already includes)
WORKDIR /app/blitz-stats
RUN mkdir data
COPY requirements.txt ./
RUN python3.11 -m pip install --no-cache-dir -U pip
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY tanks.json maps.json ./
COPY *.py ./
COPY blitzstats.ini.docker ./blitzstats.ini
CMD ["/bin/bash"]
