# coding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""CoNLL format template."""

from collections import Counter
import numpy as np
from gluonnlp.vocab import BERTVocab

import gluonnlp
from gluonnlp.model.utils import _load_vocab
from scripts.parsing.common.savable import Savable
from scripts.parsing.common.k_means import KMeans
import itertools
import mxnet.ndarray as nd


def create_bert_tokenizer(bert_vocab):
    return gluonnlp.data.BERTTokenizer(vocab=bert_vocab, lower=True)


def bert_tokenize_sentence(sentence, bert_tokenizer):
    """Apply BERT tokenizer on a tagged sentence to break words into sub-words.
    This function assumes input tags are following IOBES, and outputs IOBES tags.

    Parameters
    ----------
    sentence: List[str]
        List of tagged words
    bert_tokenizer: nlp.data.BertTokenizer
        BERT tokenizer

    Returns
    -------
    List[List[str]]: list of annotated sub-word tokens
    """
    ret = []
    for token in sentence:
        # break a word into sub-word tokens
        sub_token_texts = bert_tokenizer(token)
        ret.append(sub_token_texts)

    return ret


class ConllWord:
    """CoNLL format template, see http://anthology.aclweb.org/W/W06/W06-2920.pdf

    Parameters
    ----------
    id : int
        Token counter, starting at 1 for each new sentence.
    form : str
        Word form or punctuation symbol.
    lemma : str
        Lemma or stem (depending on the particular treebank) of word form,
        or an underscore if not available.
    cpos : str
        Coarse-grained part-of-speech tag, where the tagset depends on the treebank.
    pos : str
        Fine-grained part-of-speech tag, where the tagset depends on the treebank.
    feats : str
        Unordered set of syntactic and/or morphological features
        (depending on the particular treebank), or an underscore if not available.
    head : int
        Head of the current token, which is either a value of ID,
        or zero (’0’) if the token links to the virtual root node of the sentence.
    relation : str
        Dependency relation to the HEAD.
    phead : int
        Projective head of current token, which is either a value of ID or zero (’0’),
        or an underscore if not available.
    pdeprel : str
        Dependency relation to the PHEAD, or an underscore if not available.
    """

    def __init__(self, idx, form, lemma=None, cpos=None, pos=None, feats=None,
                 head=None, relation=None, phead=None, pdeprel=None):
        self.idx = idx
        self.form = form
        self.cpos = cpos
        self.pos = pos
        self.head = head
        self.relation = relation
        self.lemma = lemma
        self.feats = feats
        self.phead = phead
        self.pdeprel = pdeprel

    def __str__(self):
        values = [str(self.idx), self.form, self.lemma, self.cpos, self.pos, self.feats,
                  str(self.head), self.relation, self.phead, self.pdeprel]
        return '\t'.join(['_' if v is None else v for v in values])


class ConllSentence:
    """A list of ConllWord

    Parameters
    ----------
    words : ConllWord
        words of a sentence
    """

    def __init__(self, words):
        super().__init__()
        self.words = words

    def __str__(self):
        return '\n'.join([word.__str__() for word in self.words])

    def __len__(self):
        return len(self.words)

    def __getitem__(self, index):
        return self.words[index]

    def __iter__(self):
        return (line for line in self.words)


class ParserVocabulary(Savable):
    """Vocabulary, holds word, tag and relation along with their id.

    Load from conll file
    Adopted from https://github.com/jcyk/Dynet-Biaffine-dependency-parser with some modifications

    Parameters
    ----------
    input_file : str
        conll file
    pret_embeddings : tuple
        (embedding_name, source), used for gluonnlp.embedding.create(embedding_name, source)
    min_occur_count : int
        threshold of word frequency, those words with smaller frequency will be replaced by UNK
    """

    def __init__(self, input_file, pret_embeddings=None, min_occur_count=2):
        super().__init__()
        word_counter = Counter()
        tag_set = set()
        rel_set = set()

        with open(input_file) as f:
            for line in f:
                info = line.strip().split()
                if info:
                    if len(info) == 10:
                        rel_offset = 7
                    elif len(info) == 8:
                        rel_offset = 6
                    word, tag = info[1].lower(), info[3]
                    rel = info[rel_offset]
                    word_counter[word] += 1
                    tag_set.add(tag)
                    if rel != 'root':
                        rel_set.add(rel)

        self._id2word = ['<pad>', '<root>', '<unk>']
        self._id2tag = ['<pad>', '<root>', '<unk>']
        self._id2rel = ['<pad>', 'root']

        def reverse(x):
            return dict(list(zip(x, list(range(len(x))))))

        for word, count in word_counter.most_common():
            if count > min_occur_count:
                self._id2word.append(word)

        self._pret_embeddings = pret_embeddings
        self._words_in_train_data = len(self._id2word)
        if pret_embeddings:
            self._add_pret_words(pret_embeddings)
        self._id2tag += list(tag_set)
        self._id2rel += list(rel_set)

        self._word2id = reverse(self._id2word)
        self._tag2id = reverse(self._id2tag)
        self._rel2id = reverse(self._id2rel)

    PAD, ROOT, UNK = 0, 1, 2  # Padding, Root, Unknown

    def log_info(self, logger):
        """Print statistical information via the provided logger

        Parameters
        ----------
        logger : logging.Logger
            logger created using logging.getLogger()
        """
        logger.info('#words in training set: %d', self._words_in_train_data)
        logger.info('Vocab info: #words %d, #tags %d #rels %d',
                    self.vocab_size, self.tag_size, self.rel_size)

    def _add_pret_words(self, pret_embeddings):
        """Read pre-trained embedding file for extending vocabulary

        Parameters
        ----------
        pret_embeddings : tuple
            (embedding_name, source), used for gluonnlp.embedding.create(embedding_name, source)
        """
        words_in_train_data = set(self._id2word)
        pret_embeddings = gluonnlp.embedding.create(pret_embeddings[0], source=pret_embeddings[1])

        for token in pret_embeddings.idx_to_token:
            if token not in words_in_train_data:
                self._id2word.append(token)

    def has_pret_embs(self):
        """Check whether this vocabulary contains words from pre-trained embeddings

        Returns
        -------
        bool : Whether this vocabulary contains words from pre-trained embeddings
        """
        return self._pret_embeddings is not None

    def get_pret_embs(self, word_dims=None):
        """Read pre-trained embedding file

        Parameters
        ----------
        word_dims : int or None
            vector size. Use `None` for auto-infer

        Returns
        -------
        numpy.ndarray
            T x C numpy NDArray
        """
        assert self._pret_embeddings is not None, 'No pretrained file provided.'
        pret_embeddings = gluonnlp.embedding.create(self._pret_embeddings[0],
                                                    source=self._pret_embeddings[1])
        embs = [None] * len(self._id2word)
        for idx, vec in enumerate(pret_embeddings.idx_to_vec):
            embs[idx] = vec.asnumpy()
        if word_dims is None:
            word_dims = len(pret_embeddings.idx_to_vec[0])
        for idx, emb in enumerate(embs):
            if emb is None:
                embs[idx] = np.zeros(word_dims)
        pret_embs = np.array(embs, dtype=np.float32)
        return pret_embs / np.std(pret_embs)

    def get_word_embs(self, word_dims):
        """Get randomly initialized embeddings when pre-trained embeddings are used,
        otherwise zero vectors.

        Parameters
        ----------
        word_dims : int
            word vector size
        Returns
        -------
        numpy.ndarray
            T x C numpy NDArray
        """
        if self._pret_embeddings is not None:
            return np.random.randn(self.words_in_train, word_dims).astype(np.float32)
        return np.zeros((self.words_in_train, word_dims), dtype=np.float32)

    def get_tag_embs(self, tag_dims):
        """Randomly initialize embeddings for tag

        Parameters
        ----------
        tag_dims : int
            tag vector size

        Returns
        -------
        numpy.ndarray
            random embeddings
        """
        return np.random.randn(self.tag_size, tag_dims).astype(np.float32)

    def word2id(self, xs):
        """Map word(s) to its id(s)

        Parameters
        ----------
        xs : str or list
            word or a list of words

        Returns
        -------
        int or list
            id or a list of ids
        """
        if isinstance(xs, list):
            return [self._word2id.get(x, self.UNK) for x in xs]
        return self._word2id.get(xs, self.UNK)

    def id2word(self, xs):
        """Map id(s) to word(s)

        Parameters
        ----------
        xs : int
            id or a list of ids

        Returns
        -------
        str or list
            word or a list of words
        """
        if isinstance(xs, list):
            return [self._id2word[x] for x in xs]
        return self._id2word[xs]

    def rel2id(self, xs):
        """Map relation(s) to id(s)

        Parameters
        ----------
        xs : str or list
            relation

        Returns
        -------
        int or list
            id(s) of relation
        """
        if isinstance(xs, list):
            return [self._rel2id[x] for x in xs]
        return self._rel2id[xs]

    def id2rel(self, xs):
        """Map id(s) to relation(s)

        Parameters
        ----------
        xs : int
            id or a list of ids

        Returns
        -------
        str or list
            relation or a list of relations
        """
        if isinstance(xs, list):
            return [self._id2rel[x] for x in xs]
        return self._id2rel[xs]

    def tag2id(self, xs):
        """Map tag(s) to id(s)

        Parameters
        ----------
        xs : str or list
            tag or tags

        Returns
        -------
        int or list
            id(s) of tag(s)
        """
        if isinstance(xs, list):
            return [self._tag2id.get(x, self.UNK) for x in xs]
        return self._tag2id.get(xs, self.UNK)

    @property
    def words_in_train(self):
        """
        get #words in training set
        Returns
        -------
        int
            #words in training set
        """
        return self._words_in_train_data

    @property
    def vocab_size(self):
        return len(self._id2word)

    @property
    def tag_size(self):
        return len(self._id2tag)

    @property
    def rel_size(self):
        return len(self._id2rel)


def combine_words(words, tokenizer):
    offsets = [-1] * len(words)
    sent = []
    for i, token in enumerate(words):
        token = tokenizer(token)
        offsets[i] = (len(sent), len(sent) + len(token))
        sent += token
    return sent, offsets


def combine_list_of_words(words_list, tokenizer):
    strings, offsets = [], []
    for words in words_list:
        sent, offset = combine_words(words, tokenizer)
        strings.append(sent)
        offsets.append(offset)
    return strings, offsets


class DataLoader:
    """
    Load CoNLL data
    Adopted from https://github.com/jcyk/Dynet-Biaffine-dependency-parser with some modifications

    Parameters
    ----------
    input_file : str
        path to CoNLL file
    n_bkts : int
        number of buckets
    vocab : ParserVocabulary
        vocabulary object
    """

    def __init__(self, input_file, n_bkts, vocab, bert_vocab=None):
        self.vocab = vocab
        self.bert_vocab = bert_vocab
        sents = []
        sent = [[ParserVocabulary.ROOT, ParserVocabulary.ROOT, 0, ParserVocabulary.ROOT]]
        with open(input_file) as f:
            for line in f:
                info = line.strip().split()
                if info:
                    arc_offset = 5
                    rel_offset = 6
                    if len(info) == 10:
                        arc_offset = 6
                        rel_offset = 7
                    assert info[rel_offset] in vocab._rel2id, 'Relation OOV: %s' % line
                    word, tag = vocab.word2id(info[1].lower()), vocab.tag2id(info[3])
                    head, rel = int(info[arc_offset]), vocab.rel2id(info[rel_offset])
                    sent.append([word, tag, head, rel])
                else:
                    sents.append(sent)
                    sent = [[ParserVocabulary.ROOT, ParserVocabulary.ROOT, 0,
                             ParserVocabulary.ROOT]]
            if len(sent) > 1:  # last sent in file without '\n'
                sents.append(sent)

        self.samples = len(sents)
        len_counter = Counter()
        for sent in sents:
            len_counter[len(sent)] += 1
        self._bucket_lengths = KMeans(n_bkts, len_counter).splits
        self._buckets = [[] for i in range(n_bkts)]

        # bkt_idx x length x sent_idx x 4
        len2bkt = {}
        prev_length = -1
        for bkt_idx, length in enumerate(self._bucket_lengths):
            len2bkt.update(list(zip(list(range(prev_length + 1, length + 1)),
                                    [bkt_idx] * (length - prev_length))))
            prev_length = length

        self._record = []
        for sent in sents:
            bkt_idx = len2bkt[len(sent)]
            idx = len(self._buckets[bkt_idx])
            self._buckets[bkt_idx].append(sent)
            self._record.append((bkt_idx, idx))

        if bert_vocab:
            self._buckets_sub_token_seq = [[] for i in range(n_bkts)]
            self._buckets_sub_token_offset = [[] for i in range(n_bkts)]
            tokenizer = create_bert_tokenizer(bert_vocab)
            cls_id = bert_vocab[bert_vocab.cls_token]
            sep_id = bert_vocab[bert_vocab.sep_token]
            pad_id = bert_vocab[bert_vocab.padding_token]

        for bkt_idx, (bucket, length) in enumerate(zip(self._buckets, self._bucket_lengths)):
            self._buckets[bkt_idx] = np.zeros((length, len(bucket), 4), dtype=np.int32)
            for idx, sent in enumerate(bucket):
                self._buckets[bkt_idx][:len(sent), idx, :] = np.array(sent, dtype=np.int32)

            if bert_vocab:
                sent_list = []
                for idx, sent in enumerate(bucket):
                    word_list = []
                    for tp in sent:
                        word = vocab.id2word(tp[0])
                        word_list.append(word)
                    sent_list.append(word_list)

                sub_words, offsets = combine_list_of_words(sent_list, tokenizer)
                max_sent_len = len(max(sub_words, key=len))
                max_word_len = max(map(lambda se: se[1] - se[0], itertools.chain(*offsets)))
                pad_sub_word_id = gluonnlp.data.PadSequence(max_sent_len + 2, pad_val=pad_id, clip=True)
                sub_word_id_list = []
                valid_length_list = []
                for sent in sub_words:
                    ids = [cls_id] + bert_vocab[sent] + [sep_id]
                    valid_length_list.append(len(ids))
                    ids = pad_sub_word_id(ids)
                    sub_word_id_list.append(ids)

                char_offset_pad = 0
                pad_offsets = gluonnlp.data.PadSequence(max_word_len, pad_val=char_offset_pad, clip=True)
                pad_offsets_over_sent = gluonnlp.data.PadSequence(length, pad_val=[0] * max_word_len,
                                                                  clip=True)
                offset_for_sent = []
                for offset_sent in offsets:
                    offset_for_word = []
                    for start, end in offset_sent:
                        padded_offset = pad_offsets(list(range(start + 1, end + 1)))  # +1 for cls
                        offset_for_word.append(padded_offset)
                    offset_for_word = pad_offsets_over_sent(offset_for_word)
                    offset_for_sent.append(offset_for_word)

                self._buckets_sub_token_offset[bkt_idx] = np.array(offset_for_sent)
                self._buckets_sub_token_seq[bkt_idx] = np.array(sub_word_id_list)

    @property
    def idx_sequence(self):
        """Indices of sentences when enumerating data set from batches.
        Useful when retrieving the correct order of sentences

        Returns
        -------
        list
            List of ids ranging from 0 to #sent -1
        """
        return [x[1] for x in sorted(zip(self._record, list(range(len(self._record)))))]

    def get_batches(self, batch_size, shuffle=True):
        """Get batch iterator

        Parameters
        ----------
        batch_size : int
            size of one batch
        shuffle : bool
            whether to shuffle batches. Don't set to True when evaluating on dev or test set.
        Returns
        -------
        tuple
            word_inputs, tag_inputs, arc_targets, rel_targets
        """
        batches = []
        for bkt_idx, bucket in enumerate(self._buckets):
            bucket_size = bucket.shape[1]
            n_tokens = bucket_size * self._bucket_lengths[bkt_idx]
            n_splits = min(max(n_tokens // batch_size, 1), bucket_size)
            range_func = np.random.permutation if shuffle else np.arange
            for bkt_batch in np.array_split(range_func(bucket_size), n_splits):
                batches.append((bkt_idx, bkt_batch))

        if shuffle:
            np.random.shuffle(batches)

        for bkt_idx, bkt_batch in batches:
            word_inputs = self._buckets[bkt_idx][:, bkt_batch, 0]  # word_id x sent_id
            tag_inputs = self._buckets[bkt_idx][:, bkt_batch, 1]
            arc_targets = self._buckets[bkt_idx][:, bkt_batch, 2]
            rel_targets = self._buckets[bkt_idx][:, bkt_batch, 3]
            if self.bert_vocab:
                sub_words = self._buckets_sub_token_seq[bkt_idx][bkt_batch]
                offsets = self._buckets_sub_token_offset[bkt_idx][bkt_batch]
                token_types = np.zeros_like(sub_words)
                valid_lengths = np.not_equal(sub_words, 1).sum(axis=1)
                sub_words, offsets, token_types, valid_lengths = nd.array(sub_words), nd.array(offsets), nd.array(
                    token_types), nd.array(valid_lengths)
                yield word_inputs, tag_inputs, arc_targets, rel_targets, sub_words, offsets, token_types, valid_lengths
            else:
                yield word_inputs, tag_inputs, arc_targets, rel_targets


def main():
    train_file = 'data/ptb/train-debug.conllx'
    vocab = ParserVocabulary(train_file,
                             None,
                             min_occur_count=0)
    bert_vocab = _load_vocab('book_corpus_wiki_en_uncased', None, 'data', cls=BERTVocab)
    data_loader = DataLoader(train_file, 4, vocab, bert_vocab=bert_vocab)

    for word_inputs, tag_inputs, arc_targets, rel_targets, sub_word_seq, sub_word_offset, token_types, valid_lengths in data_loader.get_batches(
            5):
        print(' '.join(vocab.id2word(word_id) for word_id in word_inputs[:, 0]))
        print(' '.join(bert_vocab.idx_to_token[sub_id] for sub_id in sub_word_seq[0]))
        for offsets in sub_word_seq:
            print()
        break


if __name__ == '__main__':
    main()
