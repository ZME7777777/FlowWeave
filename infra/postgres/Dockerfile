FROM postgres:16.9-alpine3.21@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42

RUN sed -i \
      -e 's|http://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|https://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apk/repositories \
 && apk add --no-cache vim
