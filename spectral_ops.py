import tensorflow as tf
import numpy as np
import functools
import glob
import os


def linear_to_mel_weight_matrix(num_mel_bins, num_spectrogram_bins, sample_rate, lower_edge_hertz, upper_edge_hertz):
    ''' Returns a matrix to warp linear scale spectrograms to the mel scale.
    Adapted from tf.contrib.signal.linear_to_mel_weight_matrix with a minimum band width (in Hz scale) of 1.5 * freq_bin. 
    This function can be constant folded by graph optimization since there are no Tensor inputs.
    Args:
      num_mel_bins: Int, number of output frequency dimensions.
      num_spectrogram_bins: Int, number of input frequency dimensions.
      sample_rate: Int, sample rate of the audio.
      lower_edge_hertz: Float, lowest frequency to consider.
      upper_edge_hertz: Float, highest frequency to consider.
    Returns:
      Numpy float64 matrix of shape [num_spectrogram_bins, num_mel_bins].
    '''

    MEL_BREAK_FREQUENCY_HERTZ = 700.0
    MEL_HIGH_FREQUENCY_Q = 1127.0

    def mel_to_hertz(mel):
        return MEL_BREAK_FREQUENCY_HERTZ * (np.exp(mel / MEL_HIGH_FREQUENCY_Q) - 1.0)

    def hertz_to_mel(hertz):
        return MEL_HIGH_FREQUENCY_Q * np.log(1.0 + (hertz / MEL_BREAK_FREQUENCY_HERTZ))

    # HTK excludes the spectrogram DC bin.
    bands_to_zero = 1
    nyquist_hertz = sample_rate / 2.0
    linear_frequencies = np.linspace(0.0, nyquist_hertz, num_spectrogram_bins)[bands_to_zero:, np.newaxis]

    # Compute num_mel_bins triples of (lower_edge, center, upper_edge). The
    # center of each band is the lower and upper edge of the adjacent bands.
    # Accordingly, we divide [lower_edge_hertz, upper_edge_hertz] into
    # num_mel_bins + 2 pieces.
    band_edges_mel = np.linspace(hertz_to_mel(lower_edge_hertz), hertz_to_mel(upper_edge_hertz), num_mel_bins + 2)

    lower_edge_mel = band_edges_mel[0:-2]
    center_mel = band_edges_mel[1:-1]
    upper_edge_mel = band_edges_mel[2:]

    freq_res = nyquist_hertz / num_spectrogram_bins
    freq_th = 1.5 * freq_res
    for i in range(num_mel_bins):
        center_hz = mel_to_hertz(center_mel[i])
        lower_hz = mel_to_hertz(lower_edge_mel[i])
        upper_hz = mel_to_hertz(upper_edge_mel[i])
        if upper_hz - lower_hz < freq_th:
            rhs = 0.5 * freq_th / (center_hz + MEL_BREAK_FREQUENCY_HERTZ)
            dm = MEL_HIGH_FREQUENCY_Q * np.log(rhs + np.sqrt(1.0 + rhs ** 2))
            lower_edge_mel[i] = center_mel[i] - dm
            upper_edge_mel[i] = center_mel[i] + dm

    lower_edge_hz = mel_to_hertz(lower_edge_mel)[np.newaxis, :]
    center_hz = mel_to_hertz(center_mel)[np.newaxis, :]
    upper_edge_hz = mel_to_hertz(upper_edge_mel)[np.newaxis, :]

    # Calculate lower and upper slopes for every spectrogram bin.
    # Line segments are linear in the mel domain, not Hertz.
    lower_slopes = (linear_frequencies - lower_edge_hz) / (center_hz - lower_edge_hz)
    upper_slopes = (upper_edge_hz - linear_frequencies) / (upper_edge_hz - center_hz)

    # Intersect the line segments with each other and zero.
    weight_matrix = np.maximum(0.0, np.minimum(lower_slopes, upper_slopes))
    # Re-add the zeroed lower bins we sliced out above.
    weight_matrix = np.pad(weight_matrix, [[bands_to_zero, 0], [0, 0]], "constant")

    return weight_matrix


def mel_to_linear_weight_matrix(num_mel_bins, num_spectrogram_bins, sample_rate, lower_edge_hertz, upper_edge_hertz):

    weight_matrix = linear_to_mel_weight_matrix(num_mel_bins, num_spectrogram_bins, sample_rate, lower_edge_hertz, upper_edge_hertz)
    weight_matrix_t = np.transpose(weight_matrix)

    diagonal = [1.0 / x if np.abs(x) > 1.0e-8 else x for x in np.sum(np.matmul(weight_matrix, weight_matrix_t), axis=0)]
    weight_matrix = np.matmul(weight_matrix_t, np.diag(diagonal))

    return weight_matrix


def diff(inputs, axis=-1):

    begin_back = [0] * inputs.shape.rank
    begin_front = [0] * inputs.shape.rank
    begin_front[axis] = 1

    size = inputs.shape.as_list()
    size[axis] -= 1

    back = tf.slice(inputs, begin_back, size)
    front = tf.slice(inputs, begin_front, size)
    diffs = front - back

    return diffs


def unwrap(phases, discont=np.pi, axis=-1):

    diffs = diff(phases, axis=axis)
    mods = tf.mod(diffs + np.pi, 2 * np.pi) - np.pi
    indices = tf.logical_and(tf.equal(mods, -np.pi), tf.greater(diffs, 0))
    mods = tf.where(indices, tf.ones_like(mods) * np.pi, mods)
    corrects = mods - diffs
    cumsums = tf.cumsum(corrects, axis=axis)

    shape = phases.shape.as_list()
    shape[axis] = 1

    cumsums = tf.concat([tf.zeros(shape), cumsums], axis=axis)
    unwrapped = phases + cumsums

    return unwrapped


def instantaneous_frequency(phases, axis=-2):

    unwrapped = unwrap(phases, axis=axis)
    diffs = diff(unwrapped, axis=axis)

    begin = [0] * unwrapped.shape.rank

    size = unwrapped.shape.as_list()
    size[axis] = 1

    unwrapped = tf.slice(unwrapped, begin, size)
    diffs = tf.concat([unwrapped, diffs], axis=axis) / np.pi

    return diffs


def convert_to_spectrograms(waveforms, waveform_length, sample_rate, spectrogram_shape, overlap):

    def normalize(inputs, mean, std):
        return (inputs - mean) / std

    time_steps, num_freq_bins = spectrogram_shape
    frame_length = num_freq_bins * 2
    frame_step = int((1 - overlap) * frame_length)
    num_samples = frame_step * (time_steps - 1) + frame_length

    # For Nsynth dataset, we are putting all padding in the front
    # This causes edge effects in the tail
    waveforms = tf.pad(waveforms, [[0, 0], [num_samples - waveform_length, 0]])

    stfts = tf.signal.stft(
        signals=waveforms,
        frame_length=frame_length,
        frame_step=frame_step,
        window_fn=functools.partial(
            tf.signal.hann_window,
            periodic=True
        )
    )

    # discard_dc
    stfts = stfts[..., 1:]

    magnitude_spectrograms = tf.abs(stfts)
    phase_spectrograms = tf.angle(stfts)

    weight_matrix = linear_to_mel_weight_matrix(
        num_mel_bins=num_freq_bins,
        num_spectrogram_bins=num_freq_bins,
        sample_rate=sample_rate,
        lower_edge_hertz=0,
        upper_edge_hertz=sample_rate / 2
    )
    weight_matrix = tf.cast(weight_matrix, tf.float32)
    mel_magnitude_spectrograms = tf.tensordot(magnitude_spectrograms, weight_matrix, axes=1)
    mel_magnitude_spectrograms.set_shape(magnitude_spectrograms.shape[:-1].concatenate(weight_matrix.shape[-1:]))
    mel_phase_spectrograms = tf.tensordot(phase_spectrograms, weight_matrix, axes=1)
    mel_phase_spectrograms.set_shape(phase_spectrograms.shape[:-1].concatenate(weight_matrix.shape[-1:]))

    log_mel_magnitude_spectrograms = tf.log(mel_magnitude_spectrograms + 1e-6)
    mel_instantaneous_frequencies = instantaneous_frequency(mel_phase_spectrograms, axis=-2)

    log_mel_magnitude_spectrograms = normalize(log_mel_magnitude_spectrograms, -3.76, 10.05)
    mel_instantaneous_frequencies = normalize(mel_instantaneous_frequencies, 0.0, 1.0)

    return log_mel_magnitude_spectrograms, mel_instantaneous_frequencies


def convert_to_waveforms(log_mel_magnitude_spectrograms, mel_instantaneous_frequencies, waveform_length, sample_rate, spectrogram_shape, overlap):

    def unnormalize(inputs, mean, std):
        return inputs * std + mean

    time_steps, num_freq_bins = spectrogram_shape
    frame_length = num_freq_bins * 2
    frame_step = int((1 - overlap) * frame_length)
    num_samples = frame_step * (time_steps - 1) + frame_length

    log_mel_magnitude_spectrograms = unnormalize(log_mel_magnitude_spectrograms, -3.76, 10.05)
    mel_instantaneous_frequencies = unnormalize(mel_instantaneous_frequencies, 0.0, 1.0)

    mel_magnitude_spectrograms = tf.exp(log_mel_magnitude_spectrograms)
    mel_phase_spectrograms = tf.cumsum(mel_instantaneous_frequencies * np.pi, axis=-2)

    weight_matrix = mel_to_linear_weight_matrix(
        num_mel_bins=num_freq_bins,
        num_spectrogram_bins=num_freq_bins,
        sample_rate=sample_rate,
        lower_edge_hertz=0,
        upper_edge_hertz=sample_rate / 2
    )
    weight_matrix = tf.cast(weight_matrix, tf.float32)
    magnitudes = tf.tensordot(mel_magnitude_spectrograms, weight_matrix, axes=1)
    magnitudes.set_shape(mel_magnitude_spectrograms.shape[:-1].concatenate(weight_matrix.shape[-1:]))
    phase_spectrograms = tf.tensordot(mel_phase_spectrograms, weight_matrix, axes=1)
    phase_spectrograms.set_shape(mel_phase_spectrograms.shape[:-1].concatenate(weight_matrix.shape[-1:]))

    stfts = tf.complex(magnitudes, 0.0) * tf.complex(tf.cos(phase_spectrograms), tf.sin(phase_spectrograms))

    # discard_dc
    stfts = tf.pad(stfts, [[0, 0], [0, 0], [1, 0]])

    waveforms = tf.signal.inverse_stft(
        stfts=stfts,
        frame_length=frame_length,
        frame_step=frame_step,
        window_fn=tf.signal.inverse_stft_window_fn(
            frame_step=frame_step,
            forward_window_fn=functools.partial(
                tf.signal.hann_window,
                periodic=True
            )
        )
    )

    # For Nsynth dataset, we are putting all padding in the front
    # This causes edge effects in the tail
    waveforms = waveforms[:, num_samples - waveform_length:]

    return waveforms


def cross_correlation(x, y, padding="VALID", normalize=True):

    if normalize:
        x /= tf.sqrt(tf.reduce_sum(tf.square(x), axis=-1, keepdims=True))
        y /= tf.sqrt(tf.reduce_sum(tf.square(y), axis=-1, keepdims=True))

    cross_correlations = tf.map_fn(
        fn=lambda inputs: tf.squeeze(tf.nn.conv2d(
            input=inputs[0][tf.newaxis, :, tf.newaxis, tf.newaxis],
            filter=inputs[1][:, tf.newaxis, tf.newaxis, tf.newaxis],
            strides=[1, 1, 1, 1],
            padding=padding,
            data_format="NHWC",
        )),
        elems=(x, y),
        dtype=tf.float32,
        parallel_iterations=os.cpu_count(),
        swap_memory=True,
    )

    return cross_correlations


if __name__ == "__main__":

    from dataset import nsynth_input_fn
    import matplotlib.pyplot as plt

    tf.logging.set_verbosity(tf.logging.INFO)

    originals, _ = nsynth_input_fn(
        filenames=glob.glob("*.tfrecord"),
        batch_size=100,
        num_epochs=1,
        shuffle=False,
        pitches=range(24, 85),
        sources=[0]
    )

    reconstructions = convert_to_waveforms(
        *convert_to_spectrograms(
            waveforms=originals,
            waveform_length=64000,
            sample_rate=16000,
            spectrogram_shape=[128, 1024],
            overlap=0.75
        ),
        waveform_length=64000,
        sample_rate=16000,
        spectrogram_shape=[128, 1024],
        overlap=0.75
    )

    cross_correlations = cross_correlation(originals, reconstructions)

    with tf.train.SingularMonitoredSession(
        scaffold=tf.train.Scaffold(
            init_op=tf.global_variables_initializer(),
            local_init_op=tf.group(
                tf.local_variables_initializer(),
                tf.tables_initializer()
            )
        )
    ) as session:

        def generator():
            while True:
                try:
                    yield session.run([cross_correlations])
                except tf.errors.OutOfRangeError:
                    break

        plt.hist(*map(np.concatenate, zip(*generator())))
        plt.show()
