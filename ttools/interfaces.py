"""A collection of fully-specified model interfaces."""
import abc
import logging

import torch as th

from . import ModelInterface

from .utils import get_logger, plot_grad_flow


LOG = get_logger(__name__)


# HAS_AMP = False
# if th.cuda.is_available():
#     try:
#         HAS_AMP = True
#         from apex import amp, optimizers
#         LOG.info("Amp FP16 available")
#     except:
#         LOG.warn("Amp FP16 is not available")


class GANInterface(ModelInterface, abc.ABC):
    """Abstract GAN interface.

    Args:
        gen(th.nn.Module): generator.
        discrim(th.nn.Module): discriminator.
        lr(float): learning rate for both discriminator and generator.
        ncritic(int): number of discriminator updates per generator update.
        opt(str): optimizer type for both discriminator and generator.
        cuda(bool): whether or not to use CUDA.
        max_grad_norm(None or scalar): clip gradients above that threshold if
            provided.
    """
    def __init__(self, gen, discrim, lr=1e-4, ncritic=1,
                 gen_opt="rmsprop", disc_opt="rmsprop",
                 cuda=th.cuda.is_available(), gan_weight=1.0,
                 max_grad_norm=None, should_plot_grad=False):
        super(GANInterface, self).__init__()
        self.gen = gen
        self.discrim = discrim
        self.ncritic = ncritic
        self.gan_weight = gan_weight
        self.max_grad_norm = max_grad_norm
        self.should_plot_grad = should_plot_grad

        if self.gan_weight == 0:
            LOG.warning("GAN interface %s has gan_weight==0",
                        self.__class__.__name__)
            self.discrim = None

        if self.discrim is None:
            LOG.warning("Using a GAN interface (%s) with no discriminator",
                        self.__class__.__name__)
        else:
            LOG.info("Using GAN (%s) loss with weight %.5f",
                     self.__class__.__name__, self.gan_weight)

        # number of discriminator iterations
        self.iter = 0

        self.device = "cpu"
        if cuda:
            self.device = "cuda"

        self.gen.to(self.device)
        if self.discrim is not None:
            self.discrim.to(self.device)

        def make_opt(name: str, parameters, for_disc):
            if name == "sgd":
                return th.optim.SGD(parameters, lr=lr)
            elif name == "adam":
                if for_disc:
                    LOG.warn("Using a momentum-based optimizer in the discriminator,"
                             " this can be problematic.")
                return th.optim.Adam(parameters, lr=lr, betas=(0.5, 0.999))
            elif name == "rmsprop":
                return th.optim.RMSprop(parameters, lr=lr)
            else:
                raise ValueError(f"Invalid optimizer {name}")

        self.opt_g = make_opt(gen_opt, self.gen.parameters(), for_disc=False)

        if self.discrim is None:
            self.opt_d = None
        else:
            self.opt_d = make_opt(disc_opt, self.discrim.parameters(), for_disc=True)

    def training_step(self, batch):
        fwd_data = self.forward(batch)

        if not isinstance(fwd_data, dict):
            raise ValueError("Method `forward` should return a dict")

        bwd_data = self.backward(batch, fwd_data)

        if not isinstance(bwd_data, dict):
            raise ValueError("Method `forward` should return a dict")

        return dict(**fwd_data, **bwd_data)

    @abc.abstractmethod
    def forward(self, batch):
        """Abstract method that computes the generator output.

        Returns:
            dict of outputs

        Implement in derived classes.
        """
        pass

    @abc.abstractmethod
    def _discriminator_input(self, batch, fwd_data, fake=False):
        """Abstract method that selects the discriminator's input.

        The discriminator input is typically the output of the forward pass,
        `fwd_data` when testing a `fake` sample or some true data from the
        input `batch`.

        Args:
            batch: a batch of data generated by a `Dataset` class.
            fwd_data: the output of this class's `forward` method.
            fake(bool): if True we're providing a fake sample to the
                discriminator, otherwise a true example.

        Retuns:
            Tensor or list of tensors
        Implement in derived classes.
        """
        pass

    @abc.abstractmethod
    def _discriminator_gan_loss(self, fake_pred, real_pred):
        """Compute the GAN loss for the discriminator.

        Args:
            fake_pred(th.Tensor): discriminator output for the fake sample.
            real_pred(th.Tensor): discriminator output for the real sample.
        Returns:
            th.Tensor: a scalar loss value.

        Implement in derived classes.
        """
        pass

    @abc.abstractmethod
    def _generator_gan_loss(self, fake_pred, real_pred):
        """Compute the GAN loss for the generator.

        Args:
            fake_pred(th.Tensor): discriminator output for the fake sample.
            real_pred(th.Tensor): discriminator output for the real sample.
        Returns:
            th.Tensor: a scalar loss value.

        Implement in derived classes.
        """
        pass


    def _extra_generator_loss(self, batch, fwd_data):
        """Computes extra losses for the generator if needed.

        Returns:
            None or list of th.Tensor with shape [1], the total extra loss.
        """
        return None

    def _eval_d(self, d_inputs, backprop):
        """Eval the discriminators (optionally prevent backprop to inputs).

        Args:
            discrim_inputs (Tensor or list of Tensor): inputs to the
            discriminator.
            backrop: if False, the inputs are detached from the graph (e.g.
                for the discriminator update we do not update the generated
                tensors).
        Returns:
        """

        if isinstance(d_inputs, list):
            args = d_inputs
        else:  # assumes single input
            args = [d_inputs]

        # Detach the inputs to avoid backprops
        if not backprop:
            with th.no_grad():
                result = self.discrim(*args)
        else:
            result = self.discrim(*args)

        return result

    def backward(self, batch, fwd_data):
        """Generic GAN backward step.

        Alternates between `n_critic` discriminator updates and a single
        generator update. 
        Only uses `extra_generator_loss` as objective when `gan_weight==0`.
        """

        losses = self._extra_generator_loss(batch, fwd_data)
        if losses is None:
            extra_losses = []
            extra_g_loss = None
        else:
            extra_losses = [l.item() for l in losses]
            extra_g_loss = sum(losses)

        # No discriminator needed, just use the extra losses
        if self.discrim is None:
            if extra_g_loss is None:
                LOG.error("Training a GAN with no discriminator and no extra "
                          "loss: nothing to optimize!")
                raise RuntimeError("Training a GAN with no discriminator"
                                   " and no extra loss: nothing to optimize!")

            # Update the generator with only the non-GAN losses
            self.opt_g.zero_grad()
            extra_g_loss.backward()
            if self.should_plot_grad:
                plot_grad_flow(self.gen.named_parameters(), "generator (only extra loss)")
            if self.max_grad_norm is not None:
                nrm = th.nn.utils.clip_grad_norm_(self.gen.parameters(),
                                                  self.max_grad_norm)
                if nrm > self.max_grad_norm:
                    LOG.warning("Clipping generator gradients. norm = %.3f > %.3f", nrm, self.max_grad_norm)
            self.opt_g.step()

            return {"loss_g": None, "loss_d": None, "loss": extra_g_loss.item(),
                    "extra_losses": extra_losses}

        # If we reach this point, we have a discriminator

        loss_g = None
        loss_d = None
        if self.iter < self.ncritic:  # Update discriminator
            # We detach the generated samples, so that no grads propagate to
            # the generator here.
            fake_pred = self._eval_d(
                self._discriminator_input(batch, fwd_data, fake=True), False)
            real_pred = self._eval_d(
                self._discriminator_input(batch, fwd_data, fake=False), True)
            loss_d = self._update_discriminator(fake_pred, real_pred)

            self.iter += 1
        else:  # Update generator
            self.iter = 0  # reset discrim it counter

            # classify real/fake
            fake_in = self._discriminator_input(batch, fwd_data, fake=True)
            fake_pred_g = self._eval_d(fake_in, True)
            real_in = self._discriminator_input(batch, fwd_data, fake=False)
            real_pred_g = self._eval_d(real_in, True)

            loss_g = self._update_generator(fake_pred_g, real_pred_g, extra_g_loss)

        if extra_g_loss is not None:
            extra_g_loss = extra_g_loss.item()

        return {"loss_g": loss_g, "loss_d": loss_d, "loss": extra_g_loss,
                "extra_losses": extra_losses}

    def _update_discriminator(self, fake_pred, real_pred):
        """Generic discriminator update.
        """

        loss_d = self._discriminator_gan_loss(fake_pred, real_pred)

        total_loss = loss_d * self.gan_weight

        self.opt_d.zero_grad()

        total_loss.backward()
        if self.should_plot_grad:
            plot_grad_flow(self.discrim.named_parameters(), "discriminator")
        if self.max_grad_norm is not None:
            nrm = th.nn.utils.clip_grad_norm_(self.discrim.parameters(),
                                              self.max_grad_norm)
            if nrm > self.max_grad_norm:
                LOG.warning("Clipping discriminator gradients. norm = %.3f > %.3f", nrm, self.max_grad_norm)
        self.opt_d.step()

        return loss_d.item()

    def _update_generator(self, fake_pred, real_pred, extra_loss):
        """Generic generator update.

        Combines the GAN objective with extra losses if provided.

        Args:
            fake_pred(th.Tensor): output of the discriminator on fake
                predictions.
            real_pred(th.Tensor): output of the discriminator on real
                predictions.
        """
        loss_g = self._generator_gan_loss(fake_pred, real_pred)

        total_loss = loss_g * self.gan_weight

        # We have non-GAN terms in the loss
        if extra_loss is not None:
            total_loss = total_loss + extra_loss

        self.opt_g.zero_grad()
        total_loss.backward()
        if self.should_plot_grad:
            plot_grad_flow(self.gen.named_parameters(), "generator")
        if self.max_grad_norm is not None:
            nrm = th.nn.utils.clip_grad_norm_(self.gen.parameters(),
                                              self.max_grad_norm)
            if nrm > self.max_grad_norm:
                LOG.warning("Clipping generator gradients. norm = %.3f > %.3f", nrm, self.max_grad_norm)
        self.opt_g.step()

        return loss_g.item()


class SGANInterface(GANInterface):
    """Standard GAN interface [Goodfellow2014]."""
    def __init__(self, *args, **kwargs):
        super(SGANInterface, self).__init__(*args, **kwargs)
        self.cross_entropy = th.nn.BCEWithLogitsLoss()

    def _discriminator_gan_loss(self, fake_pred, real_pred):
        real_loss = self.cross_entropy(real_pred, th.ones_like(real_pred))
        fake_loss = self.cross_entropy(fake_pred, th.zeros_like(fake_pred))
        loss_d = 0.5*(fake_loss + real_loss)
        return loss_d

    def _generator_gan_loss(self, fake_pred, real_pred):
        loss_g = self.cross_entropy(fake_pred, th.ones_like(fake_pred))
        return loss_g


class RGANInterface(SGANInterface):
    """Relativistic GAN interface [Jolicoeur-Martineau2018].

    https://arxiv.org/abs/1807.00734

    """
    def _discriminator_gan_loss(self, fake_pred, real_pred):
        loss_d = self.cross_entropy(
            real_pred - fake_pred, th.ones_like(real_pred))
        return loss_d

    def _generator_gan_loss(self, fake_pred, real_pred):
        loss_g = self.cross_entropy(
            fake_pred - real_pred, th.ones_like(fake_pred))
        return loss_g


class RaGANInterface(SGANInterface):
    """Relativistic average GAN interface [Jolicoeur-Martineau2018].

    https://arxiv.org/abs/1807.00734

    """
    def _discriminator_gan_loss(self, fake_pred, real_pred):
        loss_real = self.cross_entropy(
            real_pred-fake_pred.mean(), th.ones_like(real_pred))
        loss_fake = self.cross_entropy(
            fake_pred-real_pred.mean(), th.zeros_like(fake_pred))
        loss_d = 0.5*(loss_real + loss_fake)
        return loss_d

    def _generator_gan_loss(self, fake_pred, real_pred):
        loss_real = self.cross_entropy(
            real_pred-fake_pred.mean(), th.zeros_like(real_pred))
        loss_fake = self.cross_entropy(
            fake_pred-real_pred.mean(), th.ones_like(fake_pred))
        loss_g = 0.5*(loss_real + loss_fake)
        return loss_g


class LSGANInterface(GANInterface):
    """Least-squares GAN interface [Mao2017].
    """

    def __init__(self, *args, **kwargs):
        super(LSGANInterface, self).__init__(*args, **kwargs)
        self.mse = th.nn.MSELoss()

    def _discriminator_gan_loss(self, fake_pred, real_pred):
        fake_loss = self.mse(fake_pred, th.zeros_like(fake_pred))
        real_loss = self.mse(real_pred, th.ones_like(real_pred))
        loss_d = 0.5*(fake_loss + real_loss)
        return loss_d

    def _generator_gan_loss(self, fake_pred, real_pred):
        loss_g = self.mse(fake_pred, th.ones_like(fake_pred))
        return loss_g


class RaLSGANInterface(LSGANInterface):
    """Relativistic average Least-squares GAN interface [Jolicoeur-Martineau2018].

    https://arxiv.org/abs/1807.00734

    """
    def _discriminator_gan_loss(self, fake_pred, real_pred):
        # NOTE: -1, 1 targets
        loss_real = self.mse(
            real_pred-fake_pred.mean(), th.ones_like(real_pred))
        loss_fake = self.mse(
            fake_pred-real_pred.mean(), -th.ones_like(fake_pred))
        loss_d = 0.5*(loss_real + loss_fake)
        return loss_d

    def _generator_gan_loss(self, fake_pred, real_pred):
        # NOTE: -1, 1 targets
        loss_real = self.mse(
            real_pred-fake_pred.mean(), -th.ones_like(real_pred))
        loss_fake = self.mse(
            fake_pred-real_pred.mean(), th.ones_like(fake_pred))
        loss_g = 0.5*(loss_real + loss_fake)
        return loss_g


class WGANInterface(GANInterface):
    """Wasserstein GAN.

    Args:
        c (float): clipping parameter for the Lipschitz constant
                   of the discriminator.
    """
    def __init__(self, *args, c=0.1, **kwargs):
        super(WGANInterface, self).__init__(*args, **kwargs)
        assert c > 0, "clipping param should be positive."
        self.c = c

    def _discriminator_gan_loss(self, fake_pred, real_pred):
        # minus sign for gradient ascent
        loss_d = - (real_pred.mean() - fake_pred.mean())
        return loss_d

    def _update_discriminator(self, fake_pred, real_pred):
        loss_d_scalar = super(WGANInterface, self)._update_discriminator(
            fake_pred, real_pred)

        # Clip discriminator parameters to enforce Lipschitz constraint
        for p in self.discrim.parameters():
            p.data.clamp_(-self.c, self.c)

        return loss_d_scalar

    def _generator_gan_loss(self, fake_pred, real_pred):
        loss_g = -fake_pred.mean()
        return loss_g
