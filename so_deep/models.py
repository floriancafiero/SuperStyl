import torch
import torch.nn as nn
import torch.nn.functional as F


from typing import Dict, List

class Encoder(nn.Module):
    def __init__(self, input_dim: int, device: str = "cpu"):
        super(Encoder, self).__init__()
        self.input_dim = input_dim
        self.device = device


class ConvEmbedding(Encoder):
    def __init__(self, input_dim=15000, device: str = "cpu",
                 emb_dim=128, hid_dim=128, n_layers=6, kernel_size=3, dropout=0.5):
        super().__init__(input_dim=input_dim, device=device)

        assert kernel_size % 2 == 1, "Kernel size must be odd!"

        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.kernel_size = kernel_size
        self.dropout = dropout

        self.scale = torch.sqrt(torch.FloatTensor([0.5])).to(self.device)

        self.tok_embedding = nn.Embedding(input_dim, emb_dim)

        self.emb2hid = nn.Linear(emb_dim, hid_dim)
        self.hid2emb = nn.Linear(hid_dim, emb_dim)

        self.convs = nn.ModuleList([nn.Conv1d(in_channels=hid_dim,
                                              out_channels=2 * hid_dim,
                                              kernel_size=kernel_size,
                                              padding=(kernel_size - 1) // 2)
                                    for _ in range(n_layers)])

        self.dropout = nn.Dropout(dropout)

    def forward(self, src):
        """

        :param src: Tensor(batch_size, input_dim)
        :return:
        """
        # embed tokens and positions
        tok_embedded = self.tok_embedding(src)

        # tok_embedded = pos_embedded = [batch size, src sent len, emb dim]

        # combine embeddings by elementwise summing
        embedded = self.dropout(tok_embedded)

        # embedded = [batch size, src sent len, emb dim]

        # pass embedded through linear layer to go through emb dim -> hid dim
        conv_input = self.emb2hid(embedded)

        # conv_input = [batch size, src sent len, hid dim]
        # permute for convolutional layer
        conv_input = conv_input.permute(0, 2, 1)

        # conv_input = [batch size, hid dim, src sent len]

        for i, conv in enumerate(self.convs):
            # pass through convolutional layer
            conved = conv(self.dropout(conv_input))

            # conved = [batch size, 2*hid dim, src sent len]

            # pass through GLU activation function
            conved = F.glu(conved, dim=1)

            # conved = [batch size, hid dim, src sent len]

            # apply residual connection
            conved = (conved + conv_input) * self.scale

            # conved = [batch size, hid dim, src sent len]

            # set conv_input to conved for next lo`op iteration
            conv_input = conved

        # permute and convert back to emb dim
        conved = self.hid2emb(conved.permute(0, 2, 1))

        # conved = [batch size, src sent len, emb dim]

        return conved


class LinearDecoder(nn.Module):
    """
    Simple Linear Decoder that outputs a probability distribution
    over the vocabulary

    """
    def __init__(self, enc_dim, out_dim, highway_layers=0, highway_act='relu'):
        super().__init__()
        # Encoder dimension (input dimension)
        self.out_dim = out_dim
        # highway
        self.highway = None
        # decoder output
        self.decoder = nn.Linear(enc_dim, out_dim)

        self.relu = True

    def forward(self, enc_outs):
        """

        :param enc_outs: Tensor(Encoder output dimension, Classes)
        :return:
        """
        if self.highway is not None:
            enc_outs = self.highway(enc_outs)

        linear_out = self.decoder(enc_outs)

        return linear_out


class GoodWillHunting(nn.Module):
    masked_only = True

    def __init__(
        self,
        encoder: Encoder,
        categorizer: LinearDecoder,
        device: str = "cpu",
        **kwargs
    ):
        super().__init__()

        self.encoder: Encoder = encoder
        self.decoder: LinearDecoder = categorizer
        self.device = device

        assert self.encoder.device == self.device and self.decoder.device == self.device, \
            "All devices should be the same !"

        # nll weight
        nll_weight = torch.ones(categorizer.out_dim)
        self.register_buffer('nll_weight', nll_weight)

    def forward(self, src):
        """

        :param src: Tensor(Batch_size, feature length)
        :return: Tensor(Batch_size, Classes Count)
        """

        encoder_out = self.encoder(src)
        classifier_out = self.decoder(encoder_out)

        return classifier_out

    def predict(self, src, classnames: Dict[int, str]) -> List[str, ...]:
        """ Predicts value for a given tensor

        :param src: tensor(batch size x sentence_length)
        :param classnames: Name of the classes in a dict
        :return: Reversed Batch
        """
        out = self(src, out=None)
        logits = torch.argmax(out, 2)

        with torch.cuda.device_of(logits):
            out_as_list = logits.tolist()

        return [
            classnames.get(item, "WTF ?")
            for item in out_as_list
        ]

    def train_epoch(
        self,
        src, trg, scorer: "Scorer" = None, criterion=None
    ):
        """

        :param src: tensor(batch size x features)
        :param trg: tensor(batch_size x classes)
        :param scorer: Scorer
        :param criterion: Loss System
        :return: tensor(batch_size x output_length)

        """
        output = self(src)

        scorer.register_batch(
            torch.argmax(output, 2),
            trg,
            src
        )

        loss = criterion(
            output.view(-1, self.decoder.out_dim),
            trg.view(-1)
        )

        return loss
